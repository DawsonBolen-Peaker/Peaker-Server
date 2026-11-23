"""Rows in the files table: finding new files on disk, their status, OCR pages and SharePoint record."""

import uuid
from pathlib import Path
import pandas as pd

from system.paths import DOCUMENTS_DIR, ROOTS, root_dir, file_path
from workers.processing.reconstructFile import read_pdf_id
from system.db.dbfunctions.database import (UNSCANNED, SCANNED, FAILED, DUPLICATE, SKIPPED, OCR_SCANNED, NO_OCR,
                                            OCR_BY_SCANNER, SYSTEM_VERSION, MAIN_ROOT, connect, read_df)
from system.db.dbfunctions.folders import (sharepoint_top_level_names, folder_id_for, add_folder,
                                           update_folder_flags)
from system.db.dbfunctions.helpers import (now, rel, sha256_of, is_top_level, free_name, is_pdf,
                                           is_system_file, is_accepted_file, join_departments)













def rename_top_level_file(conn, path: Path, root_folder: str, not_file_id=None) -> Path:
    """Rename a file directly inside a root (on disk) when a top-level file or
    folder from another root has its name on SharePoint. Returns its path now."""
    taken = sharepoint_top_level_names(conn, not_file_id=not_file_id)
    new_name = free_name(path.name, root_folder, taken, is_file=True)
    if new_name == path.name:
        return path
    new_path = path.with_name(new_name)
    while new_path.exists():                     # a file not in docs.db yet already has that name
        taken.add(new_name.casefold())
        new_name = free_name(path.name, root_folder, taken, is_file=True)
        new_path = path.with_name(new_name)
    path.rename(new_path)
    print(f"Renamed {root_folder}/{path.name} -> {new_name} (another root has a top-level {path.name!r})")
    return new_path



def _same_document(conn, path: Path):
    """For a file at a path the database doesn't know: ("moved", row) when it
    is a known document whose old path is gone (matched by the uuid inside the
    PDF, then by sha256), ("copy", row) when the original is still there,
    (None, None) when it is new. Also returns the sha256 and embedded uuid."""
    pdf_id = read_pdf_id(path) if path.suffix.lower() == ".pdf" else None
    digest = sha256_of(path)
    candidates = []
    if pdf_id:
        candidates += conn.execute("SELECT * FROM files WHERE uuid = ?", (pdf_id,)).fetchall()
    candidates += conn.execute("SELECT * FROM files WHERE sha256 = ?", (digest,)).fetchall()
    for row in candidates:
        if not file_path(row).is_file():
            return "moved", row, digest, pdf_id
    if candidates:
        return "copy", candidates[0], digest, pdf_id
    return None, None, digest, pdf_id


# SYSTEM_FILES / is_system_file() and is_accepted_file() are in helpers.py.




# The roots files come from are ROOTS in system/paths.py.


OCR_PAGES_TO_CHECK = 5


def has_text_layer(page) -> bool:
    """A page has a text layer when it uses a font, even if no text comes out:
    scanners often write their OCR in fonts without a Unicode map, so text
    extraction returns nothing although the text is there. An image-only scan
    uses no fonts at all."""
    return bool(page.get_fonts()) or bool(page.get_text().strip())


def pdf_facts(path: Path):
    """(ocr_status, page_count) of a PDF from one open. ocr_status is 'scanned'
    when any of its first few pages has a text layer (the scanner's OCR), 'no ocr'
    when none do. Both are None when it isn't a PDF or won't open."""
    if path.suffix.lower() != ".pdf":
        return None, None
    import pymupdf
    try:
        with pymupdf.open(path) as doc:
            for page in doc.pages(0, min(OCR_PAGES_TO_CHECK, doc.page_count)):
                if has_text_layer(page):
                    return OCR_SCANNED, doc.page_count
            return NO_OCR, doc.page_count
    except Exception as e:
        print(f"  could not check the text layer of {path.name}: {e}")
        return None, None


def ocr_status_of(path: Path):
    """pdf_facts() without the page count."""
    return pdf_facts(path)[0]




def skip_non_pdfs(conn) -> int:
    """Only PDFs go to Claude. Waiting files that aren't PDFs (Word, Excel,
    images, ...) are set to skipped, so no run sends them."""
    rows = conn.execute("SELECT file_id, current_name FROM files WHERE status IN (?, ?)",
                        (UNSCANNED, FAILED)).fetchall()
    ids = [(r["file_id"],) for r in rows if not is_pdf(r["current_name"])]
    conn.executemany("UPDATE files SET status = '" + SKIPPED + "', error = 'not a PDF; only PDFs go to Claude'"
                     " WHERE file_id = ?", ids)
    if ids:
        print(f"{len(ids)} files that aren't PDFs set to skipped (only PDFs go to Claude)")
    return len(ids)




def add_new_items() -> list[str]:
    if not DOCUMENTS_DIR.is_dir():
        raise FileNotFoundError(f"Documents folder not found: {DOCUMENTS_DIR} (check NETWORK_ROOT in .env)")
    added, checked = [], 0
    with connect() as conn:
        known_folders = {(r["root_folder"], r["relpath"])
                         for r in conn.execute("SELECT root_folder, relpath FROM folders")}
        known_files = {(r["root_folder"] or MAIN_ROOT, r["relpath"])
                       for r in conn.execute("SELECT root_folder, relpath FROM files")}
        for root in ROOTS:
            if not root.is_dir():
                print(f"Root folder not found, skipped: {root}")
                continue
            root_name = root.name
            for p in sorted(root.rglob("*")):
                relpath = rel(p, root)
                if p.is_dir():
                    if (root_name, relpath) not in known_folders:
                        add_folder(conn, relpath, root_name)
                        known_folders.add((root_name, relpath))
                    continue
                if (not p.is_file() or (root_name, relpath) in known_files or p.name.endswith(".tmp")
                        or is_system_file(p.name) or not is_accepted_file(p.name)):
                    continue
                checked += 1
                if checked == 1:
                    print(f"Checking new files in {root}...")
                elif checked % 25 == 0:
                    print(f"  {checked} new files checked")
                kind, row, digest, pdf_id = _same_document(conn, p)
                if kind == "moved":
                    if is_top_level(relpath):
                        p = rename_top_level_file(conn, p, root_name, not_file_id=row["file_id"])
                        relpath = rel(p, root)
                    conn.execute("UPDATE files SET relpath = ?, current_name = ?, folder_id = ?, root_folder = ?"
                                 " WHERE file_id = ?",
                                 (relpath, p.name, folder_id_for(conn, relpath, root_name), root_name,
                                  row["file_id"]))
                    known_files.add((root_name, relpath))
                    print(f"Moved or renamed: {row['root_folder']}/{row['relpath']} -> {root_name}/{relpath}")
                    continue
                if kind != "copy" and is_top_level(relpath):
                    original_name = p.name
                    p = rename_top_level_file(conn, p, root_name)
                    relpath = rel(p, root)
                else:
                    original_name = p.name
                taken = pdf_id and conn.execute("SELECT 1 FROM files WHERE uuid = ?", (pdf_id,)).fetchone()
                ocr_status, page_count = pdf_facts(p)
                if kind == "copy":
                    status, error = DUPLICATE, f"same content as {row['root_folder']}/{row['relpath']}"
                elif not is_pdf(p.name):
                    status, error = SKIPPED, "not a PDF; only PDFs go to Claude"
                else:
                    status, error = UNSCANNED, ""
                conn.execute(
                    "INSERT INTO files (uuid, relpath, original_name, current_name, folder_id, status, sha256, error,"
                    " added_at, ocr_status, ocr_service, system_version, root_folder, page_count)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (pdf_id if pdf_id and not taken else str(uuid.uuid4()), relpath, original_name, p.name,
                     folder_id_for(conn, relpath, root_name), status, digest, error, now(),
                     ocr_status, OCR_BY_SCANNER if ocr_status == OCR_SCANNED else None, SYSTEM_VERSION,
                     root_name, page_count))
                known_files.add((root_name, relpath))
                if kind == "copy":
                    print(f"Duplicate, not scanned: {root_name}/{relpath} (same as {row['root_folder']}/{row['relpath']})")
                elif status == UNSCANNED:
                    added.append(relpath)
        # rows for system files added before they were skipped
        junk = [r["file_id"] for r in conn.execute("SELECT file_id, current_name FROM files")
                if is_system_file(r["current_name"])]
        conn.executemany("DELETE FROM files WHERE file_id = ?", [(i,) for i in junk])
        if junk:
            print(f"Removed {len(junk)} system files (Thumbs.db and the like) from docs.db")
        requeue_failed(conn)
        skip_non_pdfs(conn)
        update_folder_flags(conn)
    return added


def requeue_failed(conn) -> int:
    """Put failed files back with the unscanned ones, so the next run sends them
    again. Their error stays on the row, so the list can say they failed last time."""
    n = conn.execute("UPDATE files SET status = ? WHERE status = ?", (UNSCANNED, FAILED)).rowcount
    if n:
        print(f"{n} files that failed last time are back with the unscanned files")
    return n


FILE_COLUMNS = """uuid AS id, relpath, root_folder, current_name AS file, original_name, status,
                  api_batch_id AS batch_id, error"""


def files_with_status(status: str) -> pd.DataFrame:
    return read_df(f"SELECT {FILE_COLUMNS} FROM files WHERE status = ? ORDER BY relpath", (status,))


def get_file(file_id: str):
    with connect() as conn:
        row = conn.execute(f"SELECT {FILE_COLUMNS} FROM files WHERE uuid = ?", (file_id,)).fetchone()
    return dict(row) if row else None



def mark_file_sharepoint(file_id: str, sp_id: str) -> None:
    """Record that a file (by uuid) is on SharePoint with id sp_id."""
    with connect() as conn:
        cur = conn.execute("UPDATE files SET uploaded_to_sharepoint = 1, sharepoint_id = ?,"
                           " sharepoint_tagged = 0 WHERE uuid = ?", (sp_id, file_id))
        if cur.rowcount == 0:
            raise ValueError(f"No file in docs.db with id: {file_id}")


def files_to_tag() -> pd.DataFrame:
    """Scanned files on SharePoint whose Department / DocumentType aren't set there yet."""
    return read_df("SELECT uuid AS id, relpath, sharepoint_id, doc_type, departments FROM files"
                   " WHERE status = ? AND uploaded_to_sharepoint = 1 AND sharepoint_tagged = 0"
                   " AND sharepoint_id IS NOT NULL ORDER BY relpath", (SCANNED,))




def set_file_tags(file_id: str, doc_type: str, departments) -> None:
    """Fill in doc_type / departments for a file scanned before docs.db kept them."""
    with connect() as conn:
        conn.execute("UPDATE files SET doc_type = ?, departments = ? WHERE uuid = ?",
                     (doc_type or "", join_departments(departments), file_id))


def mark_file_tagged(file_id: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE files SET sharepoint_tagged = 1 WHERE uuid = ?", (file_id,))



def rename_file_for_sharepoint(file_id: str, also_taken: str = "") -> Path:
    """Rename a top-level file on disk (and in docs.db) because its name is
    taken on SharePoint. Returns its new path."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM files WHERE uuid = ?", (file_id,)).fetchone()
        path = file_path(row)
        taken = sharepoint_top_level_names(conn, not_file_id=row["file_id"])
        taken |= {row["current_name"].casefold(), also_taken.casefold()}
        new_name = free_name(row["current_name"], row["root_folder"], taken, is_file=True)
        while path.with_name(new_name).exists():
            taken.add(new_name.casefold())
            new_name = free_name(row["current_name"], row["root_folder"], taken, is_file=True)
        new_path = path.with_name(new_name)
        path.rename(new_path)
        conn.execute("UPDATE files SET relpath = ?, current_name = ? WHERE file_id = ?",
                     (rel(new_path, root_dir(row["root_folder"])), new_name, row["file_id"]))
    print(f"Renamed {row['root_folder']}/{row['current_name']} -> {new_name} (the name is taken on SharePoint)")
    return new_path


def getDocsForIntel():
    with connect() as conn:
        rows = conn.execute("SELECT * FROM files WHERE ocr_service ? OR ocr_status = ? AND status = ?", ('scanner', 'none', 'unscanned')).fetchall()
    return rows


def top_level_names_for(file_id: str) -> set:
    """sharepoint_top_level_names() leaving out this file (by uuid)."""
    with connect() as conn:
        row = conn.execute("SELECT file_id FROM files WHERE uuid = ?", (file_id,)).fetchone()
        return sharepoint_top_level_names(conn, not_file_id=row["file_id"] if row else None)

def set_status(file_ids, status: str, batch_id: str = "", error: str = "") -> None:
    """Set status (plus Message Batches id / error note) on several files in one write."""
    ids = list(set(file_ids))
    if not ids:
        return
    error = " ".join(error.split())[:500]
    with connect() as conn:
        conn.executemany("UPDATE files SET status = ?, api_batch_id = ?, error = ? WHERE uuid = ?",
                         [(status, batch_id, error, i) for i in ids])
        update_folder_flags(conn)


def save_pages(file_id: str, stamp: str, pages: list[dict]) -> None:
    """Replace a file's page rows (by uuid) and record which version of the file they came from."""
    with connect() as conn:
        row = conn.execute("SELECT file_id FROM files WHERE uuid = ?", (file_id,)).fetchone()
        if row is None:
            return
        conn.execute("DELETE FROM pages WHERE file_id = ?", (row["file_id"],))
        conn.executemany(
            "INSERT INTO pages (file_id, page, score, source, ink, send_image, text) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(row["file_id"], p["page"], p["score"], p["source"], p.get("ink", 0), int(p["send_image"]), p["text"])
             for p in pages])
        conn.execute("UPDATE files SET ocr_stamp = ? WHERE file_id = ?", (stamp, row["file_id"]))




def clear_pages(file_id: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM pages WHERE file_id = (SELECT file_id FROM files WHERE uuid = ?)", (file_id,))
        conn.execute("UPDATE files SET ocr_stamp = '' WHERE uuid = ?", (file_id,))


def page_totals(file_ids) -> dict:
    """{uuid: {"pages", "images", "chars"}} for files that have been read."""
    ids = list(file_ids)
    out = {}
    with connect() as conn:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for r in conn.execute(
                    f"SELECT f.uuid, COUNT(p.page) AS pages, COALESCE(SUM(p.send_image), 0) AS images,"
                    f" COALESCE(SUM(LENGTH(p.text)), 0) AS chars FROM files f JOIN pages p ON p.file_id = f.file_id"
                    f" WHERE f.uuid IN ({marks}) GROUP BY f.uuid", chunk):
                out[r["uuid"]] = {"pages": r["pages"], "images": r["images"], "chars": r["chars"]}
    return out


def reset_failed() -> int:
    failed = files_with_status(FAILED)["id"].tolist()
    set_status(failed, UNSCANNED)
    return len(failed)

def mark_failed(file_id: str, error: str) -> None:
    """The next add_new_items() puts failed files back with the unscanned ones (requeue_failed)."""
    set_status([file_id], FAILED, error=error)
