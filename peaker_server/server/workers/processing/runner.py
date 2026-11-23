"""One entry point for "process everything": picks the Batch API or the
regular API by workload size."""
from server.system.db.dbfunctions.database import WORK_LOCK, UNSCANNED, add_new_items, files_with_status
from server.workers.processing.localScan import run_local
from server.services.Claude.batchApi import submit_to_batch
from server.services.Claude.claudeAgent import estimate_cost
from server.workers.processing.ocr import ocr_unscanned

BATCH_THRESHOLD = 5   


def _no_events(*args):
    pass


def pending_rows(file_ids=None) -> list[dict]:
    """Unscanned files, or just the ones in file_ids (a selection in the UI)."""
    with WORK_LOCK:
        add_new_items()
        pending = files_with_status(UNSCANNED)
    if file_ids is not None:
        pending = pending[pending["id"].isin(set(file_ids))]
    return pending.to_dict("records")


def pending_count() -> int:
    return len(pending_rows())


def uses_batch(n_files: int) -> bool:
    return n_files > BATCH_THRESHOLD


def read_pages(on_event=_no_events) -> None:
    """OCR any unscanned file that hasn't been read yet (usually done already,
    in the background, right after the files were found)."""
    ocr_unscanned(on_progress=lambda done, total: on_event("ocr", done, total))


def estimate(on_event=_no_events, file_ids=None) -> dict:
    """A rough price for running a batch now (every unscanned file, or just
    file_ids). Reads any pages not read yet first."""
    pending_rows()
    read_pages(on_event)
    rows = pending_rows(file_ids)
    return estimate_cost(rows, batch=uses_batch(len(rows)))


def run_all(on_event=_no_events, file_ids=None) -> str:
    """Process every unscanned file, or just file_ids. Returns "batch", "local" or "none".
    on_event also gets ("mode", "batch" | "local", n_files) when each phase starts."""
    ids = [r["id"] for r in pending_rows(file_ids)]
    n = len(ids)
    if n == 0:
        print("Nothing to scan.")
        return "none"
    if file_ids is not None:
        print(f"Running {n} selected files")
    read_pages(on_event)
    if uses_batch(n):
        on_event("mode", "batch", n)
        _, fallback = submit_to_batch(on_event=on_event, file_ids=ids)
        if fallback:
            on_event("mode", "local", len(fallback))
            run_local(file_ids=fallback, on_event=on_event)
        return "batch"
    on_event("mode", "local", n)
    run_local(file_ids=ids, on_event=on_event)
    return "local"


