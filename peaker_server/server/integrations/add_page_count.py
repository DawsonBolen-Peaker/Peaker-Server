"""Run once: add files.page_count and fill it in for every PDF, then write the
total to totalpagecount.txt (next to the server folder). Close the app first so
docs.db isn't locked. From the Doc-Server folder:

    python server/integrations/add_page_count.py

Running it again is safe; it just recounts.
"""
import sys
from pathlib import Path

# Python only looks for imports next to the script being run (integrations/),
# so add server/ before importing system / db from it.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pypdf import PdfReader
from server.system.db.dbfunctions.database import connect
from system.paths import DB_PATH, file_path

COUNT_FILE = Path(__file__).resolve().parents[2] / "totalpagecount.txt"


def isPDF(p: Path) -> bool:
    return p.suffix.lower() == ".pdf"


def createPageCountColumn(conn):
    cols = [row[1] for row in conn.execute("PRAGMA table_info(files)")]
    if "page_count" not in cols:
        conn.execute("ALTER TABLE files ADD COLUMN page_count INTEGER")
        print("Added files.page_count")


def setPageCount(conn, p: Path, file_id) -> bool:
    try:
        count = len(PdfReader(p).pages)
    except Exception as e:
        print(f"Skipped {p}: {e}")
        return False
    conn.execute("UPDATE files SET page_count = ? WHERE uuid = ?", (count, file_id))
    return True


def writeTotalPageCount(conn):
    total = conn.execute("SELECT COALESCE(SUM(page_count), 0) FROM files").fetchone()[0]
    COUNT_FILE.write_text(str(total), encoding="utf-8")
    return total


def commit_changes():
    print("docs.db:", DB_PATH)
    with connect() as conn:                 # commits at the end of the block
        createPageCountColumn(conn)
        rows = conn.execute("SELECT uuid, relpath, root_folder FROM files").fetchall()
        pdfs = [r for r in rows if isPDF(Path(r["relpath"]))]
        print(f"Counting pages in {len(pdfs)} PDFs ({len(rows) - len(pdfs)} other files left empty)...")
        counted = 0
        for n, row in enumerate(pdfs, 1):
            full_path = file_path(row)      # the file's own root (Locomotive_Scans or Product Info Library)
            if not full_path.is_file():
                print(f"Skipped {full_path}: not on disk")
            elif setPageCount(conn, full_path, row["uuid"]):
                counted += 1
            if n % 50 == 0:
                print(f"  {n} of {len(pdfs)}")
        total = writeTotalPageCount(conn)
    print(f"Counted {counted} PDFs. Total pages: {total} (saved to {COUNT_FILE})")


if __name__ == "__main__":
    commit_changes()
