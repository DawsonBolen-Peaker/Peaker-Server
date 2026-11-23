"""Every read and write of docs.db. Replaces the old tree.csv tracker.

Lists of files come back as pandas DataFrames (pd.read_sql_query) with the
column names the rest of the app already used: id (the file's uuid),
relpath, file, status, batch_id (the Message Batches id), error.

docs.db sits on a network share, so each call opens a short-lived connection
(no WAL mode; it doesn't work over SMB). Run one copy of the app at a time.
"""

import hashlib, os, shutil, sqlite3, threading, uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import pandas as pd

from server.system.db.schema import SCHEMA, ADDED_COLUMNS, INDEXES_ON_ADDED, CHUNK_SCHEMA
from system.paths import DB_PATH, DOCUMENTS_DIR, ROOTS, root_dir, file_path
from server.workers.processing.reconstructFile import read_pdf_id

UNSCANNED, SCANNED, IN_PROGRESS, FAILED, DUPLICATE = "unscanned", "scanned", "in progress", "failed", "duplicate"
SKIPPED = "skipped"      # not a PDF: never sent to Claude, uploaded to SharePoint as it is
UNKNOWN_BATCH = "unknown"

# files.chunk_status values ('' = never chunked)
CHUNK_QUEUED, CHUNK_DONE, CHUNK_FAILED = "queued", "done", "failed"

# files.ocr_status / ocr_service values (same as migrations/add_ocr_columns.py in scan-manager)
OCR_SCANNED, NO_OCR = "scanned", "no ocr"
OCR_BY_SCANNER, OCR_BY_CLAUDE = "scanner", "claude"



def _system_version():
    """SYSTEM_VERSION from .env as an int, or None when it isn't set."""
    v = os.environ.get("SYSTEM_VERSION", "").strip()
    return int(v) if v.isdigit() else None

SYSTEM_VERSION = _system_version()


WORK_LOCK = threading.RLock()

_schema_ready = False


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def connect():
    """A connection that commits when the block ends (or rolls back on an error)."""
    global _schema_ready
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        if not _schema_ready:
            conn.executescript(SCHEMA)
            add_missing_columns(conn)
            migrate_folders_to_roots(conn)
            fill_in_roots(conn)
            conn.executescript(INDEXES_ON_ADDED)
            conn.executescript(CHUNK_SCHEMA)
            _schema_ready = True
        with conn:
            yield conn
    finally:
        conn.close()


def add_missing_columns(conn) -> None:
    """Bring a docs.db made by an older version up to the current columns."""
    for table, columns in ADDED_COLUMNS.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    conn.commit()


MAIN_ROOT = DOCUMENTS_DIR.name    


def migrate_folders_to_roots(conn) -> None:
    """Older docs.db files made folders.relpath UNIQUE on its own, so the same
    folder path in two roots couldn't both have a row (INSERT OR IGNORE quietly
    merged them). Rebuild the table once with UNIQUE (root_folder, relpath),
    keeping every folder_id so files.folder_id and parent_id still point right.
    A copy of docs.db is saved next to it first."""
    if not _folders_relpath_is_unique(conn):
        return
    backup = DB_PATH.with_name(DB_PATH.stem + ".before-roots" + DB_PATH.suffix)
    conn.commit()
    if not backup.exists():
        shutil.copy2(DB_PATH, backup)
        print(f"Saved a copy of docs.db before updating the folders table: {backup}")
    cols = ("folder_id, relpath, name, parent_id, has_new_files, uploaded_to_sharepoint,"
            " sharepoint_id, root_folder, sharepoint_name")
    conn.execute("PRAGMA foreign_keys = OFF")  
    try:
        conn.executescript(f"""
            BEGIN;
            CREATE TABLE folders_new (
                folder_id     INTEGER PRIMARY KEY AUTOINCREMENT,
                relpath       TEXT NOT NULL,
                name          TEXT NOT NULL,
                parent_id     INTEGER REFERENCES folders (folder_id) ON DELETE CASCADE,
                has_new_files INTEGER NOT NULL DEFAULT 0,
                uploaded_to_sharepoint INTEGER NOT NULL DEFAULT 0,
                sharepoint_id TEXT,
                root_folder   TEXT NOT NULL DEFAULT '',
                sharepoint_name TEXT,
                UNIQUE (root_folder, relpath)
            );
            INSERT INTO folders_new ({cols}) SELECT {cols} FROM folders;
            DROP TABLE folders;
            ALTER TABLE folders_new RENAME TO folders;
            COMMIT;
        """)
        bad = conn.execute("PRAGMA foreign_key_check").fetchall()
        if bad:
            print(f"  warning: {len(bad)} rows point at a missing row after the folders update")
    finally:
        conn.execute("PRAGMA foreign_keys = ON")
    print("docs.db: folders can now have the same path in different roots")


def fill_in_roots(conn) -> None:
    """Rows from before there were several roots are all in the documents
    folder, and every folder's SharePoint name so far is its own name."""
    conn.execute("UPDATE folders SET root_folder = ? WHERE COALESCE(root_folder, '') = ''", (MAIN_ROOT,))
    conn.execute("UPDATE files SET root_folder = ? WHERE COALESCE(root_folder, '') = ''", (MAIN_ROOT,))
    conn.execute("UPDATE folders SET sharepoint_name = name WHERE sharepoint_name IS NULL")
    conn.commit()


def init_db() -> None:
    with connect():
        pass


def read_df(sql: str, params=()) -> pd.DataFrame:
    with connect() as conn:
        return pd.read_sql_query(sql, conn, params=params)


# ---------- meta ----------

def get_meta(key: str, default=None):
    with connect() as conn:
        row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(key: str, value: str, conn=None) -> None:
    sql = "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value"
    if conn is not None:
        conn.execute(sql, (key, value))
        return
    with connect() as c:
        c.execute(sql, (key, value))


# ---------- helpers ----------

# def rel(p: Path) -> str:
#     return p.relative_to(DOCUMENTS_DIR).as_posix()

#new pass in root since not everything will be relative to documents now. most will still be but 
#we need to add more directories
def rel(p: Path, r: Path) -> str:
    return p.relative_to(r).as_posix()


def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()





# ---------- finding files ----------


def get_folder(folder_id: int):
    """One folder row as a dict (folder_id, relpath, name, parent_id, ...), or None."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()
    return dict(row) if row else None


def load_folders() -> list[str]:
    """Folders Claude can file documents into: the documents folder's only.
    (processRequest only moves files that sit at the top of the documents
    folder; files in the other roots stay where they are.)"""
    with connect() as conn:
        return [r["relpath"] for r in conn.execute("SELECT relpath FROM folders WHERE root_folder = ?"
                                                   " ORDER BY relpath", (MAIN_ROOT,))]

def get_folders() -> pd.DataFrame:
    return read_df("SELECT * FROM folders ORDER BY name")


def get_folders_added_to_sharepoint() -> list[dict]:
    """[{"folder_id", "root_folder", "relpath", "name", "sharepoint_name", "sharepoint_id"}, ...]
    for folders already on SharePoint."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT folder_id, root_folder, relpath, name, sharepoint_name, sharepoint_id FROM folders"
            " WHERE uploaded_to_sharepoint = 1")]


def status_counts() -> dict:
    with connect() as conn:
        return {r["status"]: r["n"] for r in conn.execute("SELECT status, COUNT(*) AS n FROM files GROUP BY status")}


# ---------- updating files ----------




# ---------- SharePoint tracking (no uploading here, just the record) ----------
# A rescan (mark_processed) sets a file back to "not on SharePoint", since
# the copy there would be out of date.


#keep this here since it can be used for files and folders
def sharepoint_owner(sp_id: str):
    """("folder", folder_id) or ("file", uuid) of the docs.db row that has this
    SharePoint id, or None when no row does."""
    with connect() as conn:
        row = conn.execute("SELECT folder_id FROM folders WHERE sharepoint_id = ?", (sp_id,)).fetchone()
        if row:
            return "folder", row["folder_id"]
        row = conn.execute("SELECT uuid FROM files WHERE sharepoint_id = ?", (sp_id,)).fetchone()
        return ("file", row["uuid"]) if row else None





def clear_sharepoint(file_ids=(), folder_ids=()) -> None:
    """Mark files (uuids) and folders (folder_ids) as not on SharePoint again."""
    with connect() as conn:
        conn.executemany("UPDATE files SET uploaded_to_sharepoint = 0, sharepoint_id = NULL,"
                         " sharepoint_tagged = 0 WHERE uuid = ?",
                         [(i,) for i in file_ids])
        conn.executemany("UPDATE folders SET uploaded_to_sharepoint = 0, sharepoint_id = NULL WHERE folder_id = ?",
                         [(i,) for i in folder_ids])





def mark_failed(file_id: str, error: str) -> None:
    """The next add_new_items() puts failed files back with the unscanned ones (requeue_failed)."""
    set_status([file_id], FAILED, error=error)




# ---------- OCR pages (see processing/ocr.py) ----------

#I have files for doc intel now so idk if i even need this

def files_for_ocr() -> pd.DataFrame:
    """Unscanned files with their current OCR stamp ('' when never read)."""
    return read_df("SELECT uuid AS id, relpath, root_folder, ocr_stamp FROM files WHERE status = ? ORDER BY relpath",
                   (UNSCANNED,))







# ---------- file list for the UI (filter on the main file conditions) ----------

LIST_COLUMNS = """uuid AS id, relpath, root_folder, current_name AS file, status, error,
                  uploaded_to_sharepoint AS sharepoint, chunked, chunk_status, chunk_error,
                  chunk_count, page_count, doc_type"""


def _yes_no(value):
    """'yes' / 'no' / True / False / 1 / 0 -> 1 or 0; anything else (None, 'any', '') -> None."""
    if value in (True, 1, "1", "yes", "true"):
        return 1
    if value in (False, 0, "0", "no", "false"):
        return 0
    return None


# ---------- search chunks (api/claude_chunk.py, api/chunkBatchApi.py) ----------


def record_run_usage(batch_name: str, usage=None, files_done: int = 0, files_failed: int = 0) -> None:
    """Add a run's usage and file counts to its batches row (for runs that
    don't go through mark_processed, like chunking)."""
    with connect() as conn:
        _add_to_batch(conn, batch_name, usage, files_done=files_done, files_failed=files_failed)
