import anthropic
import os
import json
from services.api.search import search

MODEL = "claude-sonnet-5-5"

client = anthropic.Anthropic(api_key=os.environ.get("CLAUDE_CLIENT_SERVICE_API_KEY"))

#type of requests potentially

#question (most common)
#task: Find me a specific document
#clarifying question
#potentially more, claude will just decide this, and run it's own queries 
#web search, if absolutely nothing is returned, we search the web or potentially all of sharepoint not just peaker-drive

# how many times Claude can search: the first search, plus one more if it asks to research
MAX_SEARCH_ROUNDS = 2

SYSTEM_QUERY = """
You are a helpful assistant that answers questions about work processes found in our documents.
For this step, you will be given a question and should write your top 5 search queries to find the most relevant documents.
Return only JSON in this format:

{"queries": ["query1", "query2", "query3", "query4", "query5"]}
"""

SYSTEM_ANSWER = """
You are a helpful assistant that answers questions about work processes found in our document database.
You will be given a question and a set of document chunks with their data. Use only those chunks.

Pick one response_type:
- "answer": the chunks answer the question. Answer it using only the chunks.
- "clarify": the question is too vague or could mean several different things (for example it names no engine
  model, part or process and the chunks cover several). Put one short clarifying question in "answer".
- "research": the chunks don't answer it, but searching with different terms probably would.
  Put up to 5 new search queries in "queries" and leave "answer" empty.
If nothing relevant was found and searching again won't help, use "answer" and say so. Do not make up an answer.

Return only JSON in this format:

{
"response_type": "answer",
"answer": "the answer text, or the clarifying question",
"source_uuids": ["uuid1", "uuid2"],
"queries": []
}

Include only the chunk_uuids you actually used. Don't include more than 5, and 1 is fine.
For "clarify" and "research", source_uuids can be empty.
"""

# added to the last round, so Claude answers or asks instead of asking to search again
NO_MORE_RESEARCH = "\n\nSearching again isn't possible now: use \"answer\" or \"clarify\", not \"research\"."

RESPONSE_TYPES = ("answer", "clarify", "research")


def parse_json(text: str) -> dict:
    """Parse JSON from the model, even if it wrapped it in code fences."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)


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


QUERY_SCHEMA = {
    "type": "object",
    "properties": {"queries": {"type": "array", "items": {"type": "string"}}},
    "required": ["queries"],
    "additionalProperties": False,
}

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {
        "response_type": {"type": "string", "enum": list(RESPONSE_TYPES)},
        "answer": {"type": "string"},
        "source_uuids": {"type": "array", "items": {"type": "string"}},
        "queries": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["response_type", "answer", "source_uuids", "queries"],
    "additionalProperties": False,
}


def create_json(schema: dict, **kwargs):
    """messages.create with the reply locked to schema. An older anthropic package doesn't
    know output_config: then it's a plain call and find_json is the safety net."""
    try:
        return client.messages.create(
            output_config={"format": {"type": "json_schema", "schema": schema}},
            **kwargs,
        )
    except TypeError:
        print("anthropic package too old for structured outputs, run: pip install -U anthropic")
        return client.messages.create(**kwargs)


def first_text(response) -> str:
    """The first text block. With thinking on, content[0] can be a thinking block."""
    return next((b.text for b in response.content if b.type == "text"), "")


def create_query(question: str) -> list[str]:
    response = create_json(
        QUERY_SCHEMA,
        model=MODEL,
        max_tokens=2000,
        system=SYSTEM_QUERY,
        messages=[{"role": "user", "content": question}],
    )
    data = find_json(first_text(response)) or {}
    queries = [str(q) for q in data.get("queries") or [] if str(q).strip()]
    return queries or [question]  


def find_chunks(queries: list[str], max_chunks: int, seen: set) -> list[dict]:
    """Chunks for the queries that aren't in seen yet, at most max_chunks."""
    found = []
    for q in queries:
        for m in search(q):
            if m["chunk_uuid"] not in seen:
                seen.add(m["chunk_uuid"])
                found.append(m)
        if len(found) >= max_chunks:
            break
    return found[:max_chunks]


def answer_from_chunks(question: str, chunks: list[dict], allow_research: bool) -> dict:
    user_message = f"Question: {question}\n\nChunks:\n{json.dumps(chunks, indent=2, default=str)}"
    if not allow_research:
        user_message += NO_MORE_RESEARCH

    response = create_json(
        ANSWER_SCHEMA,
        model=MODEL,
        max_tokens=4000,  # room for thinking + the answer, 2000 could cut the JSON off
        system=SYSTEM_ANSWER,
        messages=[{"role": "user", "content": user_message}],
    )

    data = find_json(first_text(response))
    if data is None:
        # never show raw model output in the chat
        print("couldn't read Claude's answer:", response.stop_reason, first_text(response)[:500])
        return {
            "response_type": "answer",
            "answer": "Sorry, I couldn't put that answer together. Please try asking again.",
            "source_uuids": [],
            "queries": [],
        }

    response_type = data.get("response_type")
    if response_type not in RESPONSE_TYPES:
        response_type = "answer"
    # only uuids that were really in the chunks, so a made up one can't break the ref cards
    valid = {c["chunk_uuid"] for c in chunks}
    return {
        "response_type": response_type,
        "answer": str(data.get("answer") or ""),
        "source_uuids": [u for u in data.get("source_uuids") or [] if u in valid][:5],
        "queries": [str(q) for q in data.get("queries") or []][:5],
    }


def ask_claude(question: str, max_chunks: int = 10) -> dict:
    """Returns {"response_type": "answer" | "clarify", "answer": str, "source_uuids": [str]}.
    "research" is handled here: Claude gets one more search with its own queries."""
    queries = create_query(question)
    chunks = []
    seen = set()

    for round_no in range(MAX_SEARCH_ROUNDS):
        chunks += find_chunks(queries, max_chunks, seen)
        if not chunks:
            return {"response_type": "answer", "answer": "I couldn't find anything relevant in our documents.", "source_uuids": [], "sources": []}

        last_round = round_no == MAX_SEARCH_ROUNDS - 1
        result = answer_from_chunks(question, chunks, allow_research=not last_round)

        if result["response_type"] == "research" and not last_round and result["queries"]:
            queries = result["queries"]
            continue

        if result["response_type"] == "research":
            # asked to search again on the last round anyway
            result["response_type"] = "answer"
            result["answer"] = result["answer"] or "I couldn't find enough in our documents to answer that."

        # the full chunk rows Claude used (same shape as ChunkRow on the client), so the ref cards
        # get the file name and highlighted snippet without another request or any output tokens
        by_uuid = {c["chunk_uuid"]: c for c in chunks}
        result["sources"] = [by_uuid[u] for u in result["source_uuids"]]
        result.pop("queries", None)
        return result


SYSTEM_TITLE = """
Write a short title (2 to 6 words) for a chat that starts with the question below.
Return only the title: no quotes, no ending punctuation.
"""


def make_chat_title(question: str) -> str:
    response = client.messages.create(
        model=MODEL,
        max_tokens=1000,
        system=SYSTEM_TITLE,
        messages=[{"role": "user", "content": question}],
    )
    title = first_text(response).strip().strip('"').strip()
    return title[:60] or question[:40]



#what should be new:

#We should generally keep the overall split right now as is. However, let claude actually decide what to do between answering with current context or re-searching chunks again. should be too difficult to determine. then it will just continue to then give links to pages that are in the documents then i will need to wire pdf-js-dist

#also tomorrow, in file cards make it so that it says from files or from sharepoint within the search so i know, especially on the smaller search result list. that one was less obvious.
