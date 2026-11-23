"""Rows in the batches table (one per run): what each run sent, what it cost, and marking files processed."""

from pathlib import Path
import pandas as pd

from system.db.dbfunctions.database import (SCANNED, OCR_SCANNED, OCR_BY_CLAUDE, SYSTEM_VERSION, connect,
                                            read_df)
from system.db.dbfunctions.folders import folder_id_for, update_folder_flags
from system.db.dbfunctions.helpers import now, join_departments


def batch_row_id(conn, name: str, status: str = "running") -> int:
    conn.execute("INSERT OR IGNORE INTO batches (name, created_at, status) VALUES (?, ?, ?)",
                 (name, now(), status))
    return conn.execute("SELECT batch_id FROM batches WHERE name = ?", (name,)).fetchone()["batch_id"]


def add_batch(name: str) -> None:
    with connect() as conn:
        batch_row_id(conn, name)


def set_batch_status(name: str, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE batches SET status = ?, finished_at = ? WHERE name = ?", (status, now(), name))


def _add_to_batch(conn, batch_name: str, usage=None, files_done=0, files_failed=0,
                  pages=0, image_pages=0) -> None:
    """Add one file's (or one request's) usage and pages to a run's totals."""
    from services.finance.usage import cost_usd
    u = usage or {}
    batch_row_id(conn, batch_name)
    conn.execute(
        "UPDATE batches SET files_done = files_done + ?, files_failed = files_failed + ?,"
        " total_pages = total_pages + ?, image_pages = image_pages + ?, requests = requests + ?,"
        " input_tokens = input_tokens + ?, output_tokens = output_tokens + ?,"
        " cache_read_tokens = cache_read_tokens + ?, cache_write_tokens = cache_write_tokens + ?,"
        " cost_usd = cost_usd + ?, api = COALESCE(api, ?), model = COALESCE(NULLIF(model, ''), ?)"
        " WHERE name = ?",
        (files_done, files_failed, pages or 0, image_pages or 0, u.get("requests", 0),
         u.get("input_tokens", 0), u.get("output_tokens", 0), u.get("cache_read_tokens", 0),
         u.get("cache_write_tokens", 0), cost_usd(usage), u.get("api"), u.get("model", ""), batch_name))


def record_failed_usage(batch_name: str, usage=None) -> None:
    """A file failed in this run: count it, and whatever its requests cost (they are billed anyway)."""
    with connect() as conn:
        _add_to_batch(conn, batch_name, usage, files_failed=1)


def set_message_batch_ids(batch_name: str, ids) -> None:
    """The Message Batches a Batch API run took its results from."""
    with connect() as conn:
        batch_row_id(conn, batch_name)
        conn.execute("UPDATE batches SET message_batch_ids = ? WHERE name = ?",
                     (",".join(sorted(i for i in ids if i)), batch_name))


def batch_costs() -> pd.DataFrame:
    """Every run with what it sent and cost, newest first."""
    return read_df("SELECT name, api, model, status, created_at, finished_at, files_done, files_failed,"
                   " total_pages, image_pages, requests, input_tokens, output_tokens, cache_read_tokens,"
                   " cache_write_tokens, cost_usd, message_batch_ids FROM batches ORDER BY created_at DESC")








def mark_processed(file_id: str, result: dict, batch_name: str, usage=None) -> None:
    """result is what processRequest.process_document returned; usage is what
    its requests took (finance/usage.py). Both go on the file's row, and its
    pages and usage are added to the run's totals on the batches row."""
    from services.finance.usage import cost_usd
    new_relpath = result["relpath"]
    u = usage or {}
    with connect() as conn:
        found = conn.execute("SELECT file_id, root_folder, page_count FROM files WHERE uuid = ?", (file_id,)).fetchone()
        if found is None:
            raise ValueError(f"No file in docs.db with id: {file_id}")
        # what was sent, from its OCR pages (deleted below)
        sent = conn.execute("SELECT COUNT(*) AS pages, COALESCE(SUM(send_image), 0) AS images FROM pages"
                            " WHERE file_id = ?", (found["file_id"],)).fetchone()
        page_count = found["page_count"] or sent["pages"] or None
        conn.execute("UPDATE files SET page_count = ?, image_pages = ?, input_tokens = ?, output_tokens = ?,"
                     " cache_read_tokens = ?, cache_write_tokens = ?, cost_usd = ? WHERE file_id = ?",
                     (page_count, sent["images"], u.get("input_tokens"), u.get("output_tokens"),
                      u.get("cache_read_tokens"), u.get("cache_write_tokens"),
                      cost_usd(usage) if usage else None, found["file_id"]))
        _add_to_batch(conn, batch_name, usage, files_done=1, pages=page_count, image_pages=sent["images"])
        cur = conn.execute(
            "UPDATE files SET relpath = ?, current_name = ?, folder_id = ?, status = ?, batch_id = ?,"
            " api_batch_id = '', error = ?, sha256 = ?, pages_removed = ?, scanned_at = ?,"
            " uploaded_to_sharepoint = 0, sharepoint_id = NULL, doc_type = ?, departments = ?,"
            " sharepoint_tagged = 0, ocr_stamp = '',"
            # the rebuilt PDF always has a text layer; keep 'scanner' if the scanner made it
            " ocr_status = ?, ocr_service = COALESCE(ocr_service, ?), system_version = COALESCE(?, system_version)"
            " WHERE uuid = ?",
            (new_relpath, Path(new_relpath).name, folder_id_for(conn, new_relpath, found["root_folder"]), SCANNED,
             batch_row_id(conn, batch_name), result.get("note", ""), result.get("sha256"),
             ",".join(map(str, result.get("pages_removed") or [])), now(),
             result.get("doc_type") or "", join_departments(result.get("departments")),
             OCR_SCANNED, OCR_BY_CLAUDE, SYSTEM_VERSION, file_id))
        if cur.rowcount == 0:
            raise ValueError(f"No file in docs.db with id: {file_id}")
        # its OCR pages were only needed to send it and rebuild it
        conn.execute("DELETE FROM pages WHERE file_id = (SELECT file_id FROM files WHERE uuid = ?)", (file_id,))
        update_folder_flags(conn)

def record_run_usage(batch_name: str, usage=None, files_done: int = 0, files_failed: int = 0) -> None:
    """Add a run's usage and file counts to its batches row (for runs that
    don't go through mark_processed, like chunking)."""
    with connect() as conn:
        _add_to_batch(conn, batch_name, usage, files_done=files_done, files_failed=files_failed)
