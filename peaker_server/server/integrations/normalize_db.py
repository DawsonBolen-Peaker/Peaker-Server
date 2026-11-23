"""Run once: clean up the OCR columns and fill in root_folder. Back up docs.db first.
From server/:

    python integrations/normalize_db.py           # dry run: prints what it would change
    python integrations/normalize_db.py --apply   # writes to docs.db

What it sets:
  ocr_status / ocr_service  every file Claude fully processed (status 'scanned',
                            ocr_service 'claude') is left alone. Every other file
                            got its text layer from the scanner, so it gets
                            ocr_status 'scanned' and ocr_service 'scanner'
                            (this fixes the 'no ocr' and empty ones).
  root_folder               files without one get the documents folder's name
                            (Locomotive_Scans).

Before writing, it re-checks the files marked 'no ocr' or not checked with the
fixed text layer check (db.database.ocr_status_of) and prints what it finds, so
you can see the check now gets them right. Running it again is safe.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # so db / system import from server/
from server.system.db.dbfunctions.database import connect, ocr_status_of, OCR_SCANNED, OCR_BY_SCANNER, OCR_BY_CLAUDE, SCANNED
from system.paths import DB_PATH, DOCUMENTS_DIR

ROOT = DOCUMENTS_DIR.name
CLAUDE_DONE = "status = ? AND ocr_service = ?"      # left alone


def counts(conn):
    for r in conn.execute("SELECT status, COALESCE(ocr_status, '(empty)') AS ocr_status,"
                          " COALESCE(ocr_service, '(empty)') AS ocr_service, COUNT(*) AS n FROM files"
                          " GROUP BY 1, 2, 3 ORDER BY 1, 2, 3"):
        print(f"  {r['n']:5}  {r['status']:10}  ocr_status={r['ocr_status']:8}  ocr_service={r['ocr_service']}")


def main():
    apply = "--apply" in sys.argv
    print("docs.db:", DB_PATH)
    with connect() as conn:                    # connect() adds root_folder if it's missing
        print("now:")
        counts(conn)

        print("re-checking the text layer of files marked 'no ocr' or not checked:")
        for r in conn.execute("SELECT relpath, ocr_status FROM files"
                              " WHERE ocr_status IS NULL OR ocr_status != ?", (OCR_SCANNED,)):
            print(f"  {r['ocr_status'] or '(empty)':8} -> {ocr_status_of(DOCUMENTS_DIR / r['relpath']) or 'could not open'}"
                  f"   {r['relpath']}")

        fix = f"NOT ({CLAUDE_DONE}) AND (ocr_status IS NOT ? OR ocr_service IS NOT ?)"
        params = (SCANNED, OCR_BY_CLAUDE, OCR_SCANNED, OCR_BY_SCANNER)
        n_ocr = conn.execute(f"SELECT COUNT(*) FROM files WHERE {fix}", params).fetchone()[0]
        n_root = conn.execute("SELECT COUNT(*) FROM files WHERE COALESCE(root_folder, '') = ''").fetchone()[0]
        print(f"{n_ocr} files -> ocr_status '{OCR_SCANNED}', ocr_service '{OCR_BY_SCANNER}'")
        print(f"{n_root} files -> root_folder '{ROOT}'")
        if not apply:
            print("Dry run, nothing written. Run with --apply to save.")
            return
        conn.execute(f"UPDATE files SET ocr_status = ?, ocr_service = ? WHERE {fix}",
                     (OCR_SCANNED, OCR_BY_SCANNER, *params))
        conn.execute("UPDATE files SET root_folder = ? WHERE COALESCE(root_folder, '') = ''", (ROOT,))
        print("saved. now:")
        counts(conn)


if __name__ == "__main__":
    main()
