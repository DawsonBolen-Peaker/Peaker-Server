from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
from services.api.search import search
from services.api.claude_search_w_tool import ask_claude, MODEL as DEFAULT_MODEL
from services.api.claude_search import make_chat_title
from services.api.fileroutes import get_file_data_from_chunk_id, get_file_data, get_file_data_sp
from system.paths import file_path


#this whole folder is for api and client services, the other main folder processing will be for processes that automatically run in the background, like scanning and uploading to sharepoint
from system.db.dbfunctions.database import (UNSCANNED, SCANNED, IN_PROGRESS, FAILED, DUPLICATE, SKIPPED,
                         list_files, file_listing, list_folders, status_counts, sharepoint_counts, get_file_data)

STATUSES = (UNSCANNED, SCANNED, IN_PROGRESS, FAILED, DUPLICATE, SKIPPED)

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
    role: str                      
    content: str
    sources: list[dict] | None = None

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


#scan manager routes



@app.get("/files")
def get_files(status: str | None = Query(None, description=f"one of: {', '.join(STATUSES)}"),
              folder: str | None = Query(None, description="folder relpath, \"\" for the root"),
              on_sharepoint: bool | None = None,
              search: str | None = Query(None, description="part of the file name"),
              limit: int | None = Query(None, ge=1, description="leave out for every file"),
              offset: int = Query(0, ge=0)):
    """Files on the network folder with their processing and SharePoint status."""
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"Unknown status {status!r}; use one of {list(STATUSES)}")
    return list_files(status, folder, on_sharepoint, search, limit, offset)


@app.get("/files/{file_id}")
def get_file(file_id: str):
    """One file by its uuid."""
    row = file_listing(file_id)
    if row is None:
        raise HTTPException(404, f"No file with id {file_id}")
    return row



@app.get("/files/{file_id}/content")
def get_file_content(file_id: str):
    row = get_file_data(file_id)
    if not row:
        raise HTTPException(404)
    return FileResponse(file_path(row), content_disposition_type="inline")


@app.get("/folders")
def get_folders():
    """Folders on the network folder with their SharePoint status."""
    return list_folders()


@app.get("/status")
def get_status():
    """Counts for a dashboard: files per processing status, and SharePoint progress."""
    return {"files": status_counts(), "sharepoint": sharepoint_counts()}