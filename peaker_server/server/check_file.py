"""Why is a file in the state it's in? Prints everything docs.db knows about it.

    python server/check_file.py 170          (a file_id, or part of a file name)
"""
import json, sys
from system.db.dbfunctions.database import connect, get_meta
from system.paths import file_path


def main(term: str):
    with connect() as conn:
        rows = conn.execute(
            "SELECT f.*, b.name AS run_name FROM files f LEFT JOIN batches b ON b.batch_id = f.batch_id"
            " WHERE f.file_id = ? OR f.relpath LIKE ? OR f.original_name LIKE ? ORDER BY f.file_id",
            (int(term) if term.isdigit() else -1, f"%{term}%", f"%{term}%")).fetchall()
        if not rows:
            print(f"Nothing in docs.db matches {term!r}")
            return
        jobs = json.loads(get_meta("batch_jobs") or '{"files": {}}')["files"]
        for r in rows:
            pages = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(send_image), 0) AS img FROM pages"
                                 " WHERE file_id = ?", (r["file_id"],)).fetchone()
            same = conn.execute("SELECT file_id, relpath, status FROM files WHERE sha256 = ? AND file_id != ?",
                                (r["sha256"], r["file_id"])).fetchall()
            print(f"\nfile_id {r['file_id']}  ({r['uuid']})")
            print(f"  path:       {r['root_folder']}/{r['relpath']}  ({'exists' if file_path(r).is_file() else 'NOT on disk'})")
            print(f"  original:   {r['original_name']}")
            print(f"  status:     {r['status']}")
            print(f"  error:      {r['error'] or '-'}")
            print(f"  added:      {r['added_at']}   scanned: {r['scanned_at'] or '-'}   run: {r['run_name'] or '-'}")
            print(f"  Batch API:  {r['api_batch_id'] or '-'}"
                  + ("   (still waiting on a batch)" if r["uuid"] in jobs else ""))
            print(f"  pages read: {pages['n']} ({pages['img']} to send as images)")
            for s in same:
                print(f"  same content as file_id {s['file_id']}: {s['relpath']} ({s['status']})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    main(sys.argv[1])
