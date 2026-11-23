from datetime import datetime
import shutil, zipfile
from system.paths import HISTORY_DIR, OUTPUT_DIR
from server.system.db.dbfunctions.database import add_batch, set_batch_status

def start_batch(prefix="file_process_batch"):
    """A new batch: its archive folder (the batch area the originals are copied
    into before anything changes), its output folder, and its row in docs.db."""
    name = f"{prefix}_{datetime.now():%Y-%m-%d_%H%M%S}"
    archive_dir, out_dir = HISTORY_DIR / name, OUTPUT_DIR / name
    archive_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    add_batch(name)
    return name, archive_dir, out_dir

def zip_batch(name, archive_dir):
    zip_path = shutil.make_archive(str(HISTORY_DIR / name), "zip", archive_dir)
    expected = sum(1 for p in archive_dir.rglob("*") if p.is_file())
    with zipfile.ZipFile(zip_path) as z:
        files = [n for n in z.namelist() if not n.endswith("/")]
        if z.testzip() is not None or len(files) != expected:
            raise RuntimeError(f"Zip check failed for {zip_path}")
    shutil.rmtree(archive_dir)          # only after the zip is verified
    return zip_path


def finish_batch(name, archive_dir, out_dir):
    """Always called at the end of a run, even after an error or Ctrl+C:
    zips whatever originals made it into the archive. Never raises."""
    try:
        if any(p.is_file() for p in archive_dir.rglob("*")):
            print("Originals archived to:", zip_batch(name, archive_dir))
            set_batch_status(name, "done")
        else:
            shutil.rmtree(archive_dir, ignore_errors=True)
            if out_dir.is_dir() and not any(out_dir.iterdir()):
                out_dir.rmdir()
            set_batch_status(name, "empty")
    except Exception as e:
        print(f"Could not zip {archive_dir} ({e}); the originals are still in that folder.")
