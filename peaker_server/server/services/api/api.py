from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from app.search import search
# from app.ClaudeSearch.claude_search import ask_claude, make_chat_title
# the file is claude_search_w_tool.py: imports are case sensitive, even on Windows
from app.ClaudeSearch.claude_search_w_tool import ask_claude, MODEL as DEFAULT_MODEL
from app.ClaudeSearch.claude_search import make_chat_title
from app.fileroutes import get_file_data_from_chunk_id, get_file_data, get_file_data_sp
from app.paths import file_path


#this whole folder is for api and client services, the other main folder processing will be for processes that automatically run in the background, like scanning and uploading to sharepoint





app = FastAPI(title="Scan Manager")

# the Vite dev server
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173"], allow_methods=["GET", "POST", "DELETE"], allow_headers=["*"])





# @app.get("/files/{file_id}/content")
# def get_file_content(file_id: str):
#     if file_listing(file_id) is None:
#         raise HTTPException(404, f"No file with id {file_id}")
#     raise HTTPException(501, "File content isn't served yet")





class SearchRequest(BaseModel):
    q: str
    limit: int = 20

@app.post("/search")
def search_files(body: SearchRequest):
    """Search files by name."""
    return search(body.q, limit=body.limit)


class ChatMessage(BaseModel):
    role: str                      # "user", or "ai" / "assistant" for Claude's answers
    content: str
    sources: list[dict] | None = None


# what the chat's model / effort pickers can send (AI_MODELS / AI_EFFORTS in client aichat.ts).
# Anything else falls back to the defaults, so the client can't run up the bill with any model
AI_MODELS = {"claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2"}
AI_EFFORTS = {"low", "medium", "high"}


class AiSearchRequest(BaseModel):
    # the whole chat so far, the last one is the new question
    messages: list[ChatMessage] = []
    # older clients sent only the question
    q: str | None = None
    # full model id, e.g. "claude-sonnet-5-5", and "low" | "medium" | "high"
    model: str | None = None
    effort: str | None = None

@app.post("/askai")
def ask_ai(body: AiSearchRequest):
    history = [m.model_dump() for m in body.messages]
    if not history and body.q:
        history = [{"role": "user", "content": body.q, "sources": None}]
    if not history or history[-1]["role"] != "user":
        raise HTTPException(400, "messages must end with the user's question")
    model = body.model if body.model in AI_MODELS else DEFAULT_MODEL
    effort = body.effort if body.effort in AI_EFFORTS else "medium"
    return ask_claude(history, model=model, effort=effort)


class ChatTitleRequest(BaseModel):
    q: str

@app.post("/chattitle")
def chat_title(body: ChatTitleRequest):
    """A short title for a new AI chat, from its first question."""
    return {"title": make_chat_title(body.q)}


@app.get("/chunkfile/{chunk_uuid}")
def get_file(chunk_uuid: str):
    f = get_file_data_from_chunk_id(chunk_uuid)
    if f is None:
        raise HTTPException(404, f"No chunk with id {chunk_uuid}")
    return f




@app.get("/files/{file_id}/content")
def get_file_content(file_id: str):
    row = get_file_data(file_id)
    if not row:
        raise HTTPException(404)
    return FileResponse(file_path(row), content_disposition_type="inline")

@app.get("/files/sharepoint/{sharepoint_id}/content")
def get_file_content_from_sharepoint(sharepoint_id: str):
    row = get_file_data_sp(sharepoint_id)
    if not row:
        raise HTTPException(404)
    return FileResponse(file_path(row), content_disposition_type="inline")


