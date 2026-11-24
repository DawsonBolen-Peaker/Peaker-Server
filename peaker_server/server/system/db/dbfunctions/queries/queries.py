#these are specifically going to be queries especially for my client side applications

import pandas as pd

from system.db.dbfunctions.database import SCANNED, MAIN_ROOT, connect, read_df
from system.db.dbfunctions.files import FILE_COLUMNS
from system.db.dbfunctions.helpers import yes_no

# file list for the UI (filter on the main file conditions)
LIST_COLUMNS = """uuid AS id, relpath, root_folder, current_name AS file, status, error,
                  uploaded_to_sharepoint AS sharepoint, chunked, chunk_status, chunk_error,
                  chunk_count, page_count, doc_type"""


def files_not_on_sharepoint(scanned_only: bool = True) -> pd.DataFrame:
    """Files not added to SharePoint yet; by default only the ones ready to go
    up: scanned PDFs. Other file types (Word, Excel, images, ...) are tracked
    but never uploaded on their own; they go up on a person's request."""
    sql = f"SELECT {FILE_COLUMNS}, folder_id FROM files WHERE uploaded_to_sharepoint = 0"
    params = ()
    if scanned_only:
        sql += " AND status = ? AND LOWER(current_name) LIKE '%.pdf'"
        params = (SCANNED,)
    return read_df(sql + " ORDER BY root_folder, relpath", params)



def folders_not_on_sharepoint() -> pd.DataFrame:
    return read_df("SELECT * FROM folders WHERE uploaded_to_sharepoint = 0 ORDER BY relpath")


def sharepoint_counts() -> dict:
    """{"files_waiting", "files_uploaded", "files_failed", "folders_waiting", "folders_uploaded"},
    scanned PDFs only (other file types go up only on request)."""
    with connect() as conn:
        f = conn.execute("SELECT COALESCE(SUM(uploaded_to_sharepoint = 0), 0) AS waiting,"
                         " COALESCE(SUM(uploaded_to_sharepoint = 1), 0) AS uploaded,"
                         " COALESCE(SUM(uploaded_to_sharepoint = 0 AND sharepoint_error != ''), 0) AS failed"
                         " FROM files WHERE status = ? AND LOWER(current_name) LIKE '%.pdf'",
                         (SCANNED,)).fetchone()
        d = conn.execute("SELECT COALESCE(SUM(uploaded_to_sharepoint = 0), 0) AS waiting,"
                         " COALESCE(SUM(uploaded_to_sharepoint = 1), 0) AS uploaded FROM folders").fetchone()
    return {"files_waiting": f["waiting"], "files_uploaded": f["uploaded"], "files_failed": f["failed"],
            "folders_waiting": d["waiting"], "folders_uploaded": d["uploaded"]}


def get_scanned_files():
    return read_df("SELECT relpath, status FROM files WHERE status = 'scanned'")

def get_pages(file_id: str, stamp: str | None = None) -> list[dict] | None:
    """A file's pages in order, or None when not read yet (or read from a
    different version of the file, when stamp is given)."""
    with connect() as conn:
        row = conn.execute("SELECT file_id, ocr_stamp FROM files WHERE uuid = ?", (file_id,)).fetchone()
        if row is None or not row["ocr_stamp"] or (stamp is not None and row["ocr_stamp"] != stamp):
            return None
        rows = conn.execute("SELECT page, score, source, ink, send_image, text FROM pages"
                            " WHERE file_id = ? ORDER BY page", (row["file_id"],)).fetchall()
    return [dict(r) for r in rows] or None


def list_files(status=None, sharepoint=None, chunked=None, chunk_status=None, root=None,
               search=None, pdf_only=False) -> pd.DataFrame:
    where, params = [], []

    def one_of(column, value):
        values = [value] if isinstance(value, str) else list(value or [])
        values = [v for v in values if v not in (None, "", "any")]
        if values:
            where.append(f"{column} IN ({','.join('?' * len(values))})")
            params.extend(values)

    one_of("status", status)
    one_of("chunk_status", chunk_status)
    for column, value in (("uploaded_to_sharepoint", sharepoint), ("chunked", chunked)):
        flag = yes_no(value)
        if flag is not None:
            where.append(f"{column} = ?")
            params.append(flag)
    if root and root != "any":
        where.append("root_folder = ?")
        params.append(root)
    if search:
        where.append("relpath LIKE ?")
        params.append(f"%{search}%")
    if pdf_only:
        where.append("LOWER(current_name) LIKE '%.pdf'")
    sql = f"SELECT {LIST_COLUMNS} FROM files" + (" WHERE " + " AND ".join(where) if where else "")
    return read_df(sql + " ORDER BY root_folder, relpath", params)


# ---------- the scan manager app (/api/manager routes) ----------
# What the manager API sends for a file: names and statuses, never the content.
LISTING_COLUMNS = """f.uuid AS id, f.current_name AS name, f.original_name, f.relpath, f.root_folder,
                     COALESCE(d.relpath, '') AS folder, f.status, f.error,
                     f.uploaded_to_sharepoint, f.sharepoint_tagged, f.sharepoint_error, f.doc_type, f.departments,
                     f.pages_removed, f.added_at, f.scanned_at, f.ocr_service, f.ocr_status,
                     f.system_version, f.page_count, f.image_pages, f.cost_usd"""


def list_file_listings(status: str | None = None, folder: str | None = None, on_sharepoint: bool | None = None,
                       search: str | None = None, limit: int | None = None, offset: int = 0,
                       root_folder: str | None = None) -> list[dict]:
    """Files in docs.db, filtered by any of: processing status, root, folder relpath
    (that folder only, not subfolders), whether they're on SharePoint, and part of the name.
    (The scan manager's list_files; queries.list_files above is the scripts' DataFrame version.)"""
    where, params = [], []
    if status is not None:
        where.append("f.status = ?")
        params.append(status)
    if root_folder is not None:
        where.append("COALESCE(f.root_folder, ?) = ?")
        params += [MAIN_ROOT, root_folder]
    if folder is not None:
        where.append("COALESCE(d.relpath, '') = ?")
        params.append(folder)
    if on_sharepoint is not None:
        where.append("f.uploaded_to_sharepoint = ?")
        params.append(int(on_sharepoint))
    if search:
        where.append("f.current_name LIKE ?")
        params.append(f"%{search}%")
    sql = (f"SELECT {LISTING_COLUMNS} FROM files f LEFT JOIN folders d ON d.folder_id = f.folder_id"
           + (" WHERE " + " AND ".join(where) if where else "")
           + " ORDER BY f.root_folder, f.relpath LIMIT ? OFFSET ?")
    with connect() as conn:
        # LIMIT -1 is "no limit" in SQLite: the client loads every file, so its counts match docs.db
        return [dict(r) for r in conn.execute(sql, (*params, -1 if limit is None else limit, offset))]


def file_listing(file_id: str):
    """One file (by uuid) in the list_file_listings shape, or None."""
    with connect() as conn:
        row = conn.execute(f"SELECT {LISTING_COLUMNS} FROM files f LEFT JOIN folders d"
                           " ON d.folder_id = f.folder_id WHERE f.uuid = ?", (file_id,)).fetchone()
    return dict(row) if row else None


def list_folders() -> list[dict]:
    """Every folder with its SharePoint status and whether it holds unscanned files."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT folder_id, root_folder, relpath, name, sharepoint_name, parent_id, has_new_files,"
            " uploaded_to_sharepoint FROM folders ORDER BY root_folder, relpath")]


def root_names() -> list[str]:
    with connect() as conn:
        return [r["root_folder"] for r in conn.execute(
            "SELECT DISTINCT root_folder FROM files WHERE root_folder != '' ORDER BY root_folder")]