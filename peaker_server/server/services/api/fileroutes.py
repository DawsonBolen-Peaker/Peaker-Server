import sqlite3
from contextlib import closing
from pathlib import Path

# server/ is the import root (main.py runs "app.api:app" from there), so every import
# inside app/ starts with "app.", the same as api.py and claude_search.py
from services.api.search import _db_path


def get_file_data_from_chunk_id(chunk_uuid: str) -> dict | None:
    """The file (and its folder) a chunk came from. None when the chunk or file is gone."""
    # _db_path is a function: it finds docs.db on the network share
    with closing(sqlite3.connect(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        row = cur.execute(
            "SELECT file_id FROM chunks WHERE chunk_uuid = ?", (chunk_uuid,)
        ).fetchone()
        if row is None:
            return None

        file = cur.execute(
            "SELECT * FROM files WHERE file_id = ?", (row["file_id"],)
        ).fetchone()
        if file is None:
            return None
        file = dict(file)

        folder = None
        if file.get("folder_id") is not None:
            try:
                folder = cur.execute(
                    "SELECT * FROM folders WHERE folder_id = ?", (file["folder_id"],)
                ).fetchone()
            except sqlite3.OperationalError:
                folder = None  # no folders table: just leave the name out

    return {
        "file_id": file.get("file_id"),
        "uuid": file.get("uuid"),
        "current_name": file.get("current_name"),
        "folder_id": file.get("folder_id"),
        "doc_type": file.get("doc_type"),
        # the column is relpath (see search.py), rel_path was a KeyError
        "rel_path": file.get("relpath", file.get("rel_path")),
        "sharepoint_id": file.get("sharepoint_id"),
        "folder_name": folder["name"] if folder else None,
    }



def get_file_data(file_id: str):
    """relpath, root_folder and current_name of a file (by uuid); file_path() gives where it is."""
    with closing(sqlite3.connect(_db_path())) as conn:
        conn.row_factory = sqlite3.Row      # file_path() reads the row by column name
        return conn.execute("SELECT relpath, root_folder, current_name FROM files WHERE uuid = ?",
                            (file_id,)).fetchone()

def get_file_data_sp(sharepoint_id: str):
    """relpath, root_folder and current_name of a file (by sp_id); file_path() gives where it is."""
    with closing(sqlite3.connect(_db_path())) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT relpath, root_folder, current_name FROM files WHERE sharepoint_id = ?",
                            (sharepoint_id,)).fetchone()


# def get_file_blob(uuid: str, spid: str):
#     path = ''
#     with closing(sqlite3.connect(_db_path())) as conn:
#         if uuid:
#             path = conn.execute("SELECT relpath FROM files WHERE uuid = ?", 
#                              (uuid,)).fetchone()
#         else:
#             path = conn.execute("SELECT relpath FROM files WHERE sharepoint_id = ?",
#                              (spid,)).fetchone()


#     fileDataBlob = Path(path).read_bytes()





