import json
import os
from typing import Optional

import anthropic
from anthropic import beta_tool

from app.search import search
# keep find_json and first_text from your old file

MODEL = "claude-sonnet-5-5"
MAX_TOOL_ROUNDS = 6      
HISTORY_TURNS = 10       
SOURCE_CHARS = 1500      

client = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_CLIENT_SERVICE_API_KEY"))

SYSTEM = """
You are the assistant in the Peaker Services document library, an app for finding technical
and work-process information in our documents.

How to work:
- Small talk (hello, thanks, goodbye, how are you, what can you do) is not a question about the
  documents. Reply briefly and naturally without searching, use "answer", and cite nothing.
- Use search_chunks to find passages. If the results are weak, search again with different wording.
- If the user is asking about one specific document, pass its document_id to search inside it.
- If the answer is already in passages from earlier in the chat, answer from those. Don't search again.
- Use only what the passages say. Never make up an answer.

Your final reply is JSON with one response_type:
- "answer": you answered from the passages. Cite the passages you used, with show_page true.
- "document": the user wants the document itself, not a specific fact. Give one short sentence,
  cite one passage from that document, and set show_page to false.
- "clarify": the question is too vague to search well (for example it names no engine model, part
  or process). Ask one short question in "answer" and cite nothing.
- "not_found": you searched and nothing relevant exists. Say so plainly and cite nothing.

A follow-up question from the user about earlier results is just an "answer". You may cite the same
passages again or different ones. Cite at most 5 passages, and only ones you actually used.
"""

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "response_type": {"type": "string", "enum": ["answer", "document", "clarify", "not_found"]},
        "answer": {"type": "string"},
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "chunk_uuid": {"type": "string"},
                    "show_page": {"type": "boolean"},
                },
                "required": ["chunk_uuid", "show_page"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["response_type", "answer", "sources"],
    "additionalProperties": False,
}



def find_json(text: str) -> dict | None:
    """Safety net when the reply isn't pure JSON (text around it, or the JSON twice):
    the last {...} block in the text that parses. None if there isn't one."""
    decoder = json.JSONDecoder()
    found = None
    i = text.find("{")
    while i != -1:
        try:
            obj, end = decoder.raw_decode(text, i)
            if isinstance(obj, dict):
                found = obj
            i = text.find("{", end)
        except json.JSONDecodeError:
            i = text.find("{", i + 1)
    return found


def make_tools(seen: dict):
    """Tools for one request. `seen` collects every chunk Claude is shown,
    so we can check its citations against real chunks afterwards."""

    @beta_tool
    def search_chunks(query: str, document_id: Optional[str] = None) -> str:
        """Search document text for passages relevant to a query.
        Pass document_id to search inside one document, or leave it out to search everything.
        If results look weak or empty, try again with different wording.

        Args:
            query: What to look for. Rephrase it if a previous try found nothing useful.
            document_id: Optional. The file_id of a passage, to search only inside that document.
        """
        results = search(query, document_id=document_id)[:10]
        for r in results:
            seen[r["chunk_uuid"]] = r
        if not results:
            return "No matches found. Try different keywords or search without document_id."
        return json.dumps(results, default=str)

    return [search_chunks]


def build_messages(history: list[dict]) -> list[dict]:
    """Turn the client's chat into API messages. Assistant turns carry the passages they
    cited, so Claude can answer follow-ups without searching again."""
    messages = []
    for m in history[-HISTORY_TURNS:]:
        # the client saves Claude's turns as "ai", the API only knows "assistant"
        role = "assistant" if m.get("role") in ("ai", "assistant") else "user"
        content = (m.get("content") or "").strip()
        if role == "assistant" and m.get("sources"):
            shown = [json.dumps(s, default=str)[:SOURCE_CHARS] for s in m["sources"]]
            content += "\n\n[Passages shown with this answer]\n" + "\n".join(shown)
        if not content:
            continue
        # two turns in a row from the same side (a failed answer that was never saved) get joined
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += "\n\n" + content
        else:
            messages.append({"role": role, "content": content})
    while messages and messages[0]["role"] != "user":  # trimming can leave an assistant turn first
        messages.pop(0)
    return messages



def first_text(response) -> str:
    """The first text block. With thinking on, content[0] can be a thinking block."""
    return next((b.text for b in response.content if b.type == "text"), "")


def ask_claude(history: list[dict], model: str = MODEL, effort: str = "medium") -> dict:
    """history = the whole chat so far, last item is the new user question.
    model = full model id ("claude-sonnet-5-5"), effort = "low" | "medium" | "high",
    both picked by the user in the chat (api.py checks them first).
    Returns {"response_type", "answer", "sources": [chunk row + show_page]}."""
    # effort sits next to the JSON format. Thinking is left at the model's default (adaptive)
    output_config = {"format": {"type": "json_schema", "schema": RESPONSE_SCHEMA}, "effort": effort}
    # passages from earlier turns count as seen, so Claude can cite them again
    seen = {s["chunk_uuid"]: s for m in history for s in m.get("sources") or []}

    runner = client.beta.messages.tool_runner(
        model=model,
        # thinking counts toward this, and high effort / Fable think a lot
        max_tokens=16000,
        system=SYSTEM,
        tools=make_tools(seen),
        messages=build_messages(history),
        max_iterations=MAX_TOOL_ROUNDS,
        output_config=output_config,
    )
    final = runner.until_done()

    if final.stop_reason == "tool_use":
    # out of searches: answer from the passages it already found
        passages = json.dumps(list(seen.values())[-20:], default=str)
        final = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM,
            messages=build_messages(history[:-1]) + [{
                "role": "user",
                "content": f"{history[-1]['content']}\n\n"
                        f"You can't search any more. Answer from these passages, "
                        f"or use not_found if they don't help:\n{passages}",
            }],
            output_config=output_config,
        )

    data = find_json(first_text(final))

    

    if data is None:
        print("couldn't read Claude's answer:", final.stop_reason, first_text(final)[:500])
        return {"response_type": "answer", "sources": [],
                "answer": "Sorry, I couldn't put that answer together. Please try asking again."}

    sources = []
    for s in (data.get("sources") or [])[:5]:
        row = seen.get(s.get("chunk_uuid"))
        if row:  # drops any made-up uuid
            sources.append({**row, "show_page": bool(s.get("show_page"))})

    return {
        "response_type": data.get("response_type") or "answer",
        "answer": str(data.get("answer") or ""),
        "sources": sources,
    }