#these are specifically going to be queries especially for my client side applications

import pandas as pd

from system.db.dbfunctions.database import SCANNED, SKIPPED, connect, read_df
from system.db.dbfunctions.files import FILE_COLUMNS
from system.db.dbfunctions.helpers import yes_no

# file list for the UI (filter on the main file conditions)
LIST_COLUMNS = """uuid AS id, relpath, root_folder, current_name AS file, status, error,
                  uploaded_to_sharepoint AS sharepoint, chunked, chunk_status, chunk_error,
                  chunk_count, page_count, doc_type"""


def files_not_on_sharepoint(scanned_only: bool = True) -> pd.DataFrame:
    """Files not added to SharePoint yet; by default only the ones ready to go
    up: scanned, and skipped (not PDFs, which go up as they are)."""
    sql = f"SELECT {FILE_COLUMNS}, folder_id FROM files WHERE uploaded_to_sharepoint = 0"
    params = ()
    if scanned_only:
        sql += " AND status IN (?, ?)"
        params = (SCANNED, SKIPPED)
    return read_df(sql + " ORDER BY relpath", params)



def folders_not_on_sharepoint() -> pd.DataFrame:
    return read_df("SELECT * FROM folders WHERE uploaded_to_sharepoint = 0 ORDER BY relpath")


def sharepoint_counts() -> dict:
    """{"files_waiting", "files_uploaded", "folders_waiting", "folders_uploaded"}, scanned files only."""
    with connect() as conn:
        f = conn.execute("SELECT COALESCE(SUM(uploaded_to_sharepoint = 0), 0) AS waiting,"
                         " COALESCE(SUM(uploaded_to_sharepoint = 1), 0) AS uploaded"
                         " FROM files WHERE status IN (?, ?)", (SCANNED, SKIPPED)).fetchone()
        d = conn.execute("SELECT COALESCE(SUM(uploaded_to_sharepoint = 0), 0) AS waiting,"
                         " COALESCE(SUM(uploaded_to_sharepoint = 1), 0) AS uploaded FROM folders").fetchone()
    return {"files_waiting": f["waiting"], "files_uploaded": f["uploaded"],
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


def root_names() -> list[str]:
    with connect() as conn:
        return [r["root_folder"] for r in conn.execute(
            "SELECT DISTINCT root_folder FROM files WHERE root_folder != '' ORDER BY root_folder")]