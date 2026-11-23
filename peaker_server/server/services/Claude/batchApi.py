"""Big workloads through the Message Batches API (half price, results within 24h).

submit_to_batch()  sends every unscanned file, marks it "in progress" in
                   docs.db, and records the job there too (meta "batch_jobs").
check_batches()    asks the API about each recorded batch; once every batch
                   holding a file's chunks has ended it applies the results (rename, move, rebuild, sidecars, db) the
                   same way a local run does. Files whose request failed are
                   marked "failed", never put back to "unscanned".
BatchPoller        calls check_batches() on start and every 30 seconds,
                   skipping a tick while a local run is busy.

Two kinds of Message Batches come back here: scan batches (this file, meta
"batch_jobs", custom_id "<uuid>_<n>") and the standalone chunking batches
(api/chunkBatchApi.py, meta "chunk_jobs", custom_id "chunk_<uuid>_<n>").
check_batches() checks both, and each side skips the other's requests
(claude_chunk.is_chunk_request), so a result always goes to the right workflow.
"""
import json, shutil, tempfile, threading
import anthropic
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from anthropic.types.messages.batch_create_params import Request

from services.Claude.claudeAgent import (build_context, plan_chunks, build_params, extract_record,
                             merge_chunk_results, pdf_page_count, get_client)
from system.db.dbfunctions.database import WORK_LOCK, UNSCANNED, IN_PROGRESS, get_meta, set_meta
from system.db.dbfunctions.files import add_new_items, files_with_status, get_file, set_status, mark_failed
from system.db.dbfunctions.batches import mark_processed, record_failed_usage, set_message_batch_ids
from services.finance.usage import empty_usage, usage_of, add_usage, cost_usd
from system.editConfigFiles import track_new_categories
from workers.processing.processRequest import process_document
from system.batchconfig import start_batch, finish_batch
from system.paths import file_path
from services.Claude.claude_chunk import is_chunk_request, cut_scan_chunks, save_scan_chunks

# The API allows 100,000 requests / 256 MB per batch. Batches are kept well
# under that so each upload is quick; a big workload just becomes several
# batches, and one large PDF's chunks can be spread across batches too.
MAX_REQUESTS_PER_BATCH = 10_000
MAX_BATCH_BYTES = 150 * 1024 * 1024
UPLOAD_TIMEOUT = 30 * 60        # seconds allowed for one batch upload
POLL_SECONDS = 30


# ---------- jobs (meta "batch_jobs" in docs.db) ----------
# {"batches": {batch_id: {"submitted_at": ...}},
#  "files":   {file_id: {"relpath": ..., "chunks": [[start, end], ...],
#                         "chunk_batches": [batch_id per chunk]}}}

def load_jobs() -> dict:
    raw = get_meta("batch_jobs")
    return json.loads(raw) if raw else {"batches": {}, "files": {}}


def save_jobs(jobs: dict) -> None:
    set_meta("batch_jobs", json.dumps(jobs))


# ---------- submit ----------

def _custom_id(file_id: str, chunk_index: int) -> str:
    return f"{file_id}_{chunk_index}"        # file ids are uuids: 36 chars + suffix < 64


def _create_batches(requests: list) -> list:
    """Upload requests as one batch, halving and retrying if the API says the
    upload is too large. Returns [(batch_id, requests_in_it), ...]; batch_id
    is None for a single request the Batch API won't take at all."""
    client = get_client().with_options(timeout=UPLOAD_TIMEOUT)
    try:
        return [(client.messages.batches.create(requests=requests).id, requests)]
    except anthropic.APIStatusError as e:
        if e.status_code != 413:
            raise
        if len(requests) == 1:
            return [(None, requests)]
        mid = len(requests) // 2
        return _create_batches(requests[:mid]) + _create_batches(requests[mid:])


def _no_events(*args):
    pass


def submit_to_batch(limit=None, on_event=_no_events, file_ids=None) -> tuple[list[str], list[str]]:
    """Send unscanned files (or just file_ids) to the Batch API.
    Returns (new batch ids, file ids the Batch API wouldn't take). The caller
    runs that second list through the regular API (see runner.run_all).
    on_event gets ("total", n_files), ("chunk", files_sent, n_files) and
    ("file", file_id, "in progress", batch_id)."""
    with WORK_LOCK:
        add_new_items()
        pending = files_with_status(UNSCANNED)
        if file_ids is not None:
            pending = pending[pending["id"].isin(set(file_ids))]
        if limit:
            pending = pending.head(limit)
        if pending.empty:
            print("Nothing to send.")
            return [], []

        print(f"Preparing {len(pending)} files for the Batch API. Large workloads are sent as "
              f"several batches of up to {MAX_BATCH_BYTES // (1024 * 1024)} MB each.")
        ctx = build_context()
        jobs = load_jobs()
        created, sent_files, fallback = [], set(), set()
        on_event("total", len(pending))
        group, group_bytes = [], 0

        def flush():
            nonlocal group, group_bytes
            if not group:
                return
            for batch_id, reqs in _create_batches(group):
                ids = {r["custom_id"].rpartition("_")[0] for r in reqs}
                if batch_id is None:
                    # Too big for the Batch API: the regular API handles it instead.
                    for file_id in ids:
                        print(f"{jobs['files'][file_id]['relpath']}: too large for the Batch API, "
                              "will run with the regular API")
                        jobs["files"].pop(file_id, None)
                    fallback.update(ids)
                    continue
                ids -= fallback
                jobs["batches"][batch_id] = {"submitted_at": datetime.now().isoformat(timespec="seconds")}
                for r in reqs:
                    file_id, _, idx = r["custom_id"].rpartition("_")
                    if file_id in jobs["files"]:
                        jobs["files"][file_id]["chunk_batches"][int(idx)] = batch_id
                save_jobs(jobs)                     # record first, so a crash can't lose the job
                set_status(ids, IN_PROGRESS, batch_id=batch_id)
                created.append(batch_id)
                sent_files.update(ids)
                for file_id in ids:
                    on_event("file", file_id, IN_PROGRESS, batch_id)
                on_event("chunk", len(sent_files), len(pending))
                print(f"Submitted batch {len(created)} ({batch_id}): {len(reqs)} requests "
                      f"from {len(ids)} files. {len(sent_files)} of {len(pending)} files sent so far.")
            group, group_bytes = [], 0

        temp_root = Path(tempfile.mkdtemp(prefix="file_scanner_batch_"))
        try:
            for row in pending.to_dict("records"):
                file_id = row["id"]
                try:
                    path = file_path(row)
                    if not path.is_file():
                        raise FileNotFoundError(f"File not found: {path}")
                    chunks = plan_chunks(path, temp_root / file_id)
                    total = pdf_page_count(path)
                    requests = [
                        Request(custom_id=_custom_id(file_id, i),
                                params=build_params(ctx, p, row, s, e, total))
                        for i, (p, s, e) in enumerate(chunks)
                    ]
                except Exception as e:
                    print("FAILED:", row["relpath"], "-", e)
                    mark_failed(file_id, str(e))
                    on_event("file", file_id, "failed", str(e))
                    continue
                finally:
                    shutil.rmtree(temp_root / file_id, ignore_errors=True)

                jobs["files"][file_id] = {"relpath": row["relpath"],
                                          "chunks": [[s, e] for _, s, e in chunks],
                                          "chunk_batches": [None] * len(chunks)}
                for r in requests:                  # a file's chunks may land in different batches
                    size = len(json.dumps(r))
                    if group and (group_bytes + size > MAX_BATCH_BYTES
                                  or len(group) >= MAX_REQUESTS_PER_BATCH):
                        flush()
                    group.append(r)
                    group_bytes += size
            flush()
        except Exception as e:
            # Files with chunks that never got uploaded can't finish; fail them
            # (their other chunks' results are ignored) and keep the rest.
            stuck = [fid for fid, info in jobs["files"].items() if None in info["chunk_batches"]]
            for fid in stuck:
                jobs["files"].pop(fid)
                mark_failed(fid, f"Batch submission stopped: {e}")
                on_event("file", fid, "failed", f"Batch submission stopped: {e}")
            save_jobs(jobs)
            print(f"Batch submission stopped: {e}")
            if stuck:
                print(f"{len(stuck)} files were not fully sent and are marked failed (f = retry).")
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)
        # Files with a rejected chunk may have other chunks in a batch already;
        # those results are ignored, and the file is scanned with the regular API.
        set_status(fallback, UNSCANNED)
        sent_files -= fallback
        save_jobs(jobs)
        left = len(pending) - len(sent_files) - len(fallback)
        print(f"Done: {len(sent_files)} files sent in {len(created)} batches"
              + (f", {len(fallback)} going to the regular API" if fallback else "")
              + (f", {left} not sent (see above)" if left else "") + ".")
        return created, sorted(fallback)


# ---------- results ----------

def _error_text(result) -> str:
    """Readable reason for a request that did not succeed."""
    if result.type == "errored":
        err = getattr(getattr(result, "error", None), "error", None)
        if err is not None:
            return f"batch request errored: {getattr(err, 'type', '')} {getattr(err, 'message', '')}".strip()
        return "batch request errored"
    return f"batch request {result.type}"          # canceled / expired


def apply_results(file_ids: list, jobs: dict, on_event=_no_events) -> tuple[int, int]:
    """Apply results for files whose batches have all ended. Returns (processed, failed)."""
    client = get_client()
    wanted = set(file_ids)
    outcomes = defaultdict(dict)                    # file_id -> {chunk_index: record | error str}
    usage = defaultdict(lambda: empty_usage("batch"))   # file_id -> what its requests were billed
    batch_ids = {b for fid in wanted for b in jobs["files"][fid]["chunk_batches"]}
    for batch_id in sorted(batch_ids):
        for item in client.messages.batches.results(batch_id):
            if is_chunk_request(item.custom_id):
                continue                            # a chunking request: chunkBatchApi handles those
            file_id, _, idx = item.custom_id.rpartition("_")
            if file_id not in wanted:
                continue
            if item.result.type == "succeeded":
                add_usage(usage[file_id], usage_of(item.result.message, "batch"))
                try:
                    outcomes[file_id][int(idx)] = extract_record(item.result.message, item.custom_id)
                except Exception as e:
                    outcomes[file_id][int(idx)] = str(e)
            else:
                outcomes[file_id][int(idx)] = _error_text(item.result)

    folders = build_context().folders
    name, archive_dir, out_dir = start_batch("batch_api")
    set_message_batch_ids(name, batch_ids)
    run_usage = empty_usage("batch")
    done = failed = 0
    try:
        for file_id in file_ids:
            info = jobs["files"][file_id]
            got = outcomes.get(file_id, {})
            try:
                row = get_file(file_id)
                if row is None:
                    raise ValueError("No longer in docs.db")
                if row["status"] != IN_PROGRESS:
                    continue                        # already applied (e.g. an earlier, interrupted check)
                add_usage(run_usage, usage[file_id])
                errors = [v for v in got.values() if isinstance(v, str)]
                missing = len(info["chunks"]) - len(got)
                if errors or missing:
                    raise RuntimeError("; ".join(errors) or f"{missing} chunk results missing")
                relpath = row["relpath"]
                result = merge_chunk_results([(got[i], s, e) for i, (s, e) in enumerate(info["chunks"])])
                done_info = process_document(file_id, relpath, result, folders, archive_dir, out_dir,
                                             root_folder=row["root_folder"])
                chunks = cut_scan_chunks(file_id, result, done_info["pages_removed"])  # before its pages are cleared
                # the file is rebuilt and moved now, so record it before anything else can fail
                mark_processed(file_id, done_info, name, usage=usage[file_id])
                save_scan_chunks(file_id, chunks, result)
                try:
                    track_new_categories(result.get("categories", []) + result.get("new_categories", []))
                except Exception as e:
                    print("  could not save new categories:", e)
                done += 1
                print("batch done:", done_info["relpath"])
                on_event("file", file_id, "scanned", done_info["relpath"])
            except Exception as e:
                failed += 1
                print("batch FAILED:", info["relpath"], "-", e)
                mark_failed(file_id, str(e))
                try:
                    record_failed_usage(name, usage[file_id] if file_id in usage else None)
                except Exception as e2:
                    print("  could not record its usage in docs.db:", e2)
                on_event("file", file_id, "failed", str(e))
    finally:
        finish_batch(name, archive_dir, out_dir)
        if run_usage["requests"]:
            print(f"Tokens: {run_usage['input_tokens']:,} in, {run_usage['output_tokens']:,} out,"
                  f" {run_usage['cache_read_tokens']:,} cache read, {run_usage['cache_write_tokens']:,}"
                  f" cache write, about ${cost_usd(run_usage):.2f} (Batch API)")
    return done, failed


def check_batches(verbose=False, on_event=_no_events) -> dict:
    """Check scan batches and chunking batches once. The scan progress keys as
    in check_scan_batches(), plus the chunking ones prefixed chunk_
    (chunk_batches, chunk_files_waiting, chunk_applied, ...)."""
    from services.Claude.chunkBatchApi import check_chunk_batches
    progress = check_scan_batches(verbose, on_event)
    try:
        chunk = check_chunk_batches(verbose, on_event)
    except Exception as e:
        print(f"[chunk batch check failed: {e}]")
        chunk = {"batches": 0, "requests_done": 0, "requests_total": 0, "files_waiting": 0, "applied": None}
    progress.update({f"chunk_{k}": v for k, v in chunk.items()})
    return progress


def check_scan_batches(verbose=False, on_event=_no_events) -> dict:
    """Check every recorded scan batch once and apply files whose batches have all ended.
    Returns progress for a UI: {"batches", "requests_done", "requests_total", "files_waiting"}."""
    jobs = load_jobs()
    progress = {"batches": 0, "requests_done": 0, "requests_total": 0, "files_waiting": 0,
                "applied": None}             # (processed, failed) when results were applied this check
    if not jobs["batches"]:
        if verbose:
            print("No Batch API jobs waiting.")
        return progress
    client = get_client()
    ended, counts = set(), {}
    for batch_id in jobs["batches"]:
        batch = client.messages.batches.retrieve(batch_id)
        c = batch.request_counts
        finished = c.succeeded + c.errored + c.canceled + c.expired
        counts[batch_id] = (finished, finished + c.processing)
        if batch.processing_status == "ended":
            ended.add(batch_id)
        elif verbose:
            print(f"Batch {batch_id}: {batch.processing_status} "
                  f"({c.processing} processing, {c.succeeded} succeeded, {c.errored} errored)")

    ready = [fid for fid, info in jobs["files"].items() if set(info["chunk_batches"]) <= ended]
    if ready:
        print(f"\n{len(ready)} files have Batch API results; applying them...")
        done, failed = apply_results(ready, jobs, on_event)
        for fid in ready:
            jobs["files"].pop(fid)
        print(f"Batch API results: {done} processed, {failed} failed")
        progress["applied"] = (done, failed)

    # Forget ended batches that no waiting file still needs.
    still_needed = {b for info in jobs["files"].values() for b in info["chunk_batches"]}
    for batch_id in ended - still_needed:
        jobs["batches"].pop(batch_id)
    save_jobs(jobs)
    progress["batches"] = len(jobs["batches"])
    progress["requests_done"] = sum(counts[b][0] for b in jobs["batches"] if b in counts)
    progress["requests_total"] = sum(counts[b][1] for b in jobs["batches"] if b in counts)
    progress["files_waiting"] = len(jobs["files"])
    return progress


class BatchPoller(threading.Thread):
    """Checks Batch API jobs on start and every POLL_SECONDS, but never while
    a local run (or anything else holding WORK_LOCK) is working.
    on_progress gets check_batches()'s progress dict after each check."""

    def __init__(self, interval=POLL_SECONDS, on_progress=_no_events, on_event=_no_events):
        super().__init__(daemon=True)
        self.interval = interval
        self.on_progress = on_progress
        self.on_event = on_event
        self._stop_event = threading.Event()

    def run(self):
        while not self._stop_event.is_set():
            if WORK_LOCK.acquire(blocking=False):
                try:
                    self.on_progress(check_batches(on_event=self.on_event))
                except Exception as e:
                    print(f"\n[batch check failed: {e}]")
                finally:
                    WORK_LOCK.release()
            self._stop_event.wait(self.interval)

    def stop(self):
        self._stop_event.set()


def in_progress_count() -> int:
    return len(files_with_status(IN_PROGRESS))
