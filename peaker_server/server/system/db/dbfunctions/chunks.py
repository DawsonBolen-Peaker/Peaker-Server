"""Search chunks (chunks table) and each file's chunking status (claude_chunk.py, chunkBatchApi.py)."""

from system.db.dbfunctions.database import SCANNED, CHUNK_QUEUED, CHUNK_DONE, CHUNK_FAILED, connect
from system.db.dbfunctions.files import FILE_COLUMNS
from system.db.dbfunctions.helpers import now


def files_by_uuid(file_ids) -> list[dict]:
    """Rows (FILE_COLUMNS plus what chunking needs) for these uuids, in no set order."""
    ids = list(dict.fromkeys(file_ids))
    out = []
    with connect() as conn:
        for i in range(0, len(ids), 500):
            part = ids[i:i + 500]
            out += [dict(r) for r in conn.execute(
                f"SELECT {FILE_COLUMNS}, sha256, doc_type, departments, doc_title, doc_summary,"
                f" page_count, chunked, chunk_status FROM files WHERE uuid IN ({','.join('?' * len(part))})",
                part)]
    return out


def set_chunk_status(file_ids, status: str, batch_id: str = "", error: str = "") -> None:
    ids = list(set(file_ids))
    error = " ".join((error or "").split())[:500]
    with connect() as conn:
        conn.executemany("UPDATE files SET chunk_status = ?, chunk_batch_id = ?, chunk_error = ? WHERE uuid = ?",
                         [(status, batch_id, error, i) for i in ids])


def chunk_failed(file_id: str, error: str, clear: bool = False) -> None:
    """Chunking a file failed. clear=True also deletes its old chunks (they no
    longer match the file, e.g. after a rescan) and sets chunked back to 0."""
    with connect() as conn:
        if clear:
            conn.execute("DELETE FROM chunks WHERE file_id = (SELECT file_id FROM files WHERE uuid = ?)", (file_id,))
            conn.execute("UPDATE files SET chunked = 0, chunk_count = 0 WHERE uuid = ?", (file_id,))
        conn.execute("UPDATE files SET chunk_status = ?, chunk_batch_id = '', chunk_error = ? WHERE uuid = ?",
                     (CHUNK_FAILED, " ".join(str(error).split())[:500], file_id))


def set_doc_meta(conn, file_row_id: int, title: str, summary: str, keywords) -> None:
    conn.execute("UPDATE files SET doc_title = ?, doc_summary = ?, doc_keywords = ? WHERE file_id = ?",
                 (title or "", summary or "", "; ".join(k for k in keywords or [] if k), file_row_id))


def save_chunks(file_id: str, chunks: list[dict], source: str, model: str = "", version: int = 1,
                doc: dict | None = None, cost=None) -> int:
    """Replace a file's chunks (by uuid) with these, in one transaction, and mark
    it chunked. Each chunk: chunk_uuid, chunk_index, page_start, page_end,
    start_line, end_line, text, token_count, section_title, summary, keywords
    (JSON text), chunk_type. doc: {"title", "summary", "keywords"} to store on
    the file (and copy onto each chunk for search). Returns how many were saved."""
    with connect() as conn:
        row = conn.execute("SELECT file_id, doc_title, doc_keywords FROM files WHERE uuid = ?", (file_id,)).fetchone()
        if row is None:
            raise ValueError(f"No file in docs.db with id: {file_id}")
        if doc:
            set_doc_meta(conn, row["file_id"], doc.get("title"), doc.get("summary"), doc.get("keywords"))
            row = conn.execute("SELECT file_id, doc_title, doc_keywords FROM files WHERE file_id = ?",
                               (row["file_id"],)).fetchone()
        conn.execute("DELETE FROM chunks WHERE file_id = ?", (row["file_id"],))
        stamp = now()
        conn.executemany(
            "INSERT INTO chunks (chunk_uuid, file_id, chunk_index, page_start, page_end, start_line, end_line,"
            " text, token_count, section_title, summary, keywords, chunk_type, doc_title, doc_keywords,"
            " source, model, chunker_version, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(c["chunk_uuid"], row["file_id"], c["chunk_index"], c.get("page_start"), c.get("page_end"),
              c.get("start_line"), c.get("end_line"), c["text"], c.get("token_count"),
              c.get("section_title") or "", c.get("summary") or "", c.get("keywords") or "[]",
              c.get("chunk_type") or "", row["doc_title"] or "", row["doc_keywords"] or "",
              source, model or "", version, stamp) for c in chunks])
        conn.execute("UPDATE files SET chunked = ?, chunk_status = ?, chunk_error = '', chunk_batch_id = '',"
                     " chunked_at = ?, chunk_count = ?, chunk_cost_usd = COALESCE(?, chunk_cost_usd) WHERE file_id = ?",
                     (1 if chunks else 0, CHUNK_DONE, stamp, len(chunks), cost, row["file_id"]))
    return len(chunks)


def chunk_counts() -> dict:
    """{"chunked", "queued", "failed", "scanned_not_chunked"} over all files."""
    with connect() as conn:
        r = conn.execute(
            "SELECT COALESCE(SUM(chunked = 1), 0) AS chunked,"
            " COALESCE(SUM(chunk_status = ?), 0) AS queued,"
            " COALESCE(SUM(chunk_status = ?), 0) AS failed,"
            " COALESCE(SUM(status = ? AND chunked = 0), 0) AS scanned_not_chunked FROM files",
            (CHUNK_QUEUED, CHUNK_FAILED, SCANNED)).fetchone()
    return dict(r)
