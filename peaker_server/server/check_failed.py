"""Find failed (or requeued) files that were actually processed. From server/:

    python check_failed.py           # dry run: prints what it finds
    python check_failed.py --apply   # marks those files scanned in docs.db

A rebuild writes the file's uuid into the PDF (/DocServerId), so a failed or
requeued file whose PDF carries its own uuid finished processing; only the
status update afterwards went wrong. The rest stay queued to run again.
"""
import sys

from server.system.db.dbfunctions.database import connect, FAILED, SCANNED, UNSCANNED, update_folder_flags
from server.workers.processing.reconstructFile import read_pdf_id
from system.paths import file_path


def main():
    apply = "--apply" in sys.argv
    with connect() as conn:
        print("files by status (on SharePoint / not):")
        for r in conn.execute("SELECT status, SUM(uploaded_to_sharepoint = 1) AS up, SUM(uploaded_to_sharepoint = 0) AS down,"
                              " SUM(error != '') AS with_error FROM files GROUP BY status"):
            print(f"  {r['status']:12} {r['up']:5} on / {r['down']:5} not   ({r['with_error']} have an error)")
        rows = conn.execute("SELECT uuid, relpath, root_folder, status FROM files"
                            " WHERE status = ? OR (status = ? AND error != '')", (FAILED, UNSCANNED)).fetchall()
        done = [r for r in rows
                if (p := file_path(r)).suffix.lower() == ".pdf" and p.is_file()
                and read_pdf_id(p) == r["uuid"]]
        print(f"{len(rows)} failed or requeued files, {len(done)} of them were actually processed:")
        for r in done:
            print(f"  {r['status']:10} {r['relpath']}")
        if not apply:
            print("Dry run, nothing written. Run with --apply to save.")
            return
        conn.executemany("UPDATE files SET status = ?, error = '' WHERE uuid = ?", [(SCANNED, r["uuid"]) for r in done])
        update_folder_flags(conn)
        print("saved")


if __name__ == "__main__":
    main()
