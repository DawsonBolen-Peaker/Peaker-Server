"""Full-text search over the search chunks (chunks_fts in docs.db).

    python db/search.py "fuel injector 16-645E3"
    python db/search.py "catalytic converter" --type drawing --limit 5

search() ranks with bm25, weighting section titles and keywords above body
text, and returns the file, pages and a highlighted snippet for each hit.
A starting point for the search work; the UI doesn't use it yet.
"""
import sqlite3
import sys
from contextlib import closing
from pathlib import Path
import os
from dotenv import load_dotenv
import json, re

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))     # server/

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=True)     # server/.env

def _db_path() -> Path:
    r"""docs.db on the network share. DOCS_DB_DIR wins if set; otherwise the db folder
    under NETWORK_ROOT (or \\NETWORK_IP\PSIMain\PSI_Files, like the old paths.py)."""
    if os.environ.get("DOCS_DB_DIR", "").strip():
        return Path(os.environ["DOCS_DB_DIR"].strip()) / "docs.db"
    root = os.environ.get("NETWORK_ROOT", "").strip()
    if not root and os.environ.get("NETWORK_IP", "").strip():
        root = rf"\\{os.environ['NETWORK_IP'].strip()}\PSIMain\PSI_Files"
    if not root:
        raise RuntimeError("Set NETWORK_ROOT (or NETWORK_IP / DOCS_DB_DIR) in server/.env")
    for folder in ("doc_db", "docs_db"):
        path = Path(root) / folder / "docs.db"
        if path.exists():
            return path
    raise RuntimeError(f"docs.db not found in {root}\\doc_db or {root}\\docs_db")

WEIGHTS = (1.0, 4.0, 2.0, 3.0, 2.0, 1.0)

def fts_query(text: str, any_word: bool = False) -> str:
    """User text -> an FTS5 query: each word quoted (so - . / in part numbers
    can't break the syntax) and prefix matched, all required unless any_word."""
    words = re.findall(r'[^\s"]+', text or "")
    return (" OR " if any_word else " ").join(f'"{w}"*' for w in words)

def search(text: str, limit: int = 20, chunk_type: str | None = None, any_word: bool = False,
           document_id: str | None = None) -> list[dict]:
    """Best chunks for text. Falls back to matching any word when every word finds nothing.
    document_id: only chunks from that file (the file_id in each result, files.uuid)."""
    query = fts_query(text, any_word)
    if not query:
        return []
    where, params = ["chunks_fts MATCH ?"], [query]
    if chunk_type:
        where.append("c.chunk_type = ?")
        params.append(chunk_type)
    if document_id:
        where.append("f.uuid = ?")
        params.append(document_id)
    sql = f"""
        SELECT f.uuid AS file_id, f.root_folder, f.relpath, f.current_name AS file, f.doc_type, f.sharepoint_id,
               c.chunk_uuid, c.chunk_index, c.page_start, c.page_end, c.section_title, c.summary,
               c.keywords, c.chunk_type,
               snippet(chunks_fts, 0, '<c0>', '</c0>', ' ... ', 24) AS snippet,
               bm25(chunks_fts, {', '.join(map(str, WEIGHTS))}) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN files f ON f.file_id = c.file_id
        WHERE {' AND '.join(where)}
        ORDER BY score LIMIT ?"""
    with closing(sqlite3.connect(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params + [limit])]
    if not rows and not any_word and len(query.split()) > 1:
        return search(text, limit, chunk_type, any_word=True, document_id=document_id)
    for r in rows:
        r["keywords"] = json.loads(r["keywords"] or "[]")
    return rows


def lookup_by_title(title: str):
    with closing(sqlite3.connect(_db_path())) as conn:
        conn.execute()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--type", help="only this chunk_type")
    a = ap.parse_args()
    for r in search(a.query, a.limit, a.type):
        pages = f"p{r['page_start']}" + (f"-{r['page_end']}" if r["page_end"] != r["page_start"] else "")
        print(f"{r['score']:.2f}  {r['root_folder']}/{r['relpath']}  {pages}  [{r['chunk_type']}] {r['section_title']}")
        print(f"       {r['snippet']}\n")
