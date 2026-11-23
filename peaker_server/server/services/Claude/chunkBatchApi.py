"""The standalone chunking job: search chunks for files that are already
scanned (and usually on SharePoint), through the Message Batches API.

Their OCR pages were cleared when they were scanned, so the text comes from
the rebuilt PDF's own text layer (api/claude_chunk.read_pdf_pages). Claude
only answers with line ranges and metadata; the text is cut here.

submit_chunk_batch(ids)  sends the files, sets chunk_status = queued, and
                         records the job in docs.db (meta "chunk_jobs").
check_chunk_batches()    asks the API about each recorded chunk batch and,
                         once a file's batches have ended, cuts and saves its
                         chunks. batchApi.check_batches() calls it, so the
                         app's 30 second poller picks these up too.
chunk_now(ids)           the same with the regular API, straight away (full
                         price; for a few files, or to try it out).

Scan batches and chunk batches are kept apart: their own meta key here, and
every chunk request's custom_id starts with "chunk_" (claude_chunk.is_chunk_request).

From a terminal (in the server folder):
    python api/chunkBatchApi.py --sharepoint            chunk every scanned file on SharePoint not chunked yet
    python api/chunkBatchApi.py --sharepoint --limit 20
    python api/chunkBatchApi.py --file <uuid> --now     one file through the regular API
    python api/chunkBatchApi.py --check                 check chunk batches once
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # server/

import json, math, threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from anthropic.types.messages.batch_create_params import Request

from server.services.Claude.claude_chunk import (CUSTOM_ID_PREFIX, CHUNK_MODEL, CHUNKER_VERSION, is_chunk_request,
                              read_pdf_pages, read_pdf_meta, has_text, plan_parts, build_request,
                              extract_chunks, rows_from_parts)
from server.system.db.dbfunctions.database import (WORK_LOCK, SCANNED, CHUNK_QUEUED, get_meta, set_meta, files_by_uuid,
                         set_chunk_status, chunk_failed, save_chunks, add_batch, set_batch_status,
                         set_message_batch_ids, record_run_usage, list_files)
from server.services.finance.usage import empty_usage, usage_of, add_usage, cost_usd
from system.paths import file_path

JOBS_KEY = "chunk_jobs"
MAX_REQUESTS_PER_BATCH = 10_000
MAX_BATCH_BYTES = 150 * 1024 * 1024
WORKERS = 4                      # chunk_now threads


def _no_events(*args):
    pass


# ---------- jobs (meta "chunk_jobs" in docs.db) ----------
# {"batches": {batch_id: {"submitted_at": ...}},
#  "files":   {uuid: {"relpath": ..., "sha256": ..., "parts": [[first, last], ...],
#                     "part_batches": [batch_id per part]}}}

def load_jobs() -> dict:
    raw = get_meta(JOBS_KEY)
    return json.loads(raw) if raw else {"batches": {}, "files": {}}


def save_jobs(jobs: dict) -> None:
    set_meta(JOBS_KEY, json.dumps(jobs))


def _custom_id(file_id: str, part: int) -> str:
    return f"{CUSTOM_ID_PREFIX}{file_id}_{part}"          # 6 + 36 + 1 + digits, under the 64 allowed


def _split_custom_id(custom_id: str) -> tuple[str, int]:
    file_id, _, part = custom_id[len(CUSTOM_ID_PREFIX):].rpartition("_")
    return file_id, int(part)


# ---------- which files ----------

def is_chunkable(row: dict) -> str:
    """'' when the file can be chunked, else why not."""
    if row["status"] != SCANNED:
        return f"not scanned ({row['status']})"
    if not row["file"].lower().endswith(".pdf"):
        return "not a PDF"
    if row.get("chunk_status") == CHUNK_QUEUED:
        return "already waiting at the Batch API"
    return ""


def sharepoint_ids(include_chunked: bool = False) -> list[str]:
    """Every scanned PDF that is on SharePoint (and not chunked yet, by default)."""
    df = list_files(status=SCANNED, sharepoint="yes", chunked=None if include_chunked else "no", pdf_only=True)
    return df["id"].tolist()


def _prepare(row: dict):
    """(pages, meta, parts) for one file, read from its rebuilt PDF."""
    path = file_path(row)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    pages = read_pdf_pages(path)
    if not pages:
        raise ValueError("PDF has no pages")
    if not has_text(pages):
        raise ValueError("no text layer to chunk (rescan it to get one)")
    return pages, read_pdf_meta(path), plan_parts(pages)


def _doc(meta: dict) -> dict:
    return {"title": meta.get("title"), "summary": meta.get("summary"), "keywords": meta.get("keywords")}


# ---------- estimate ----------

CHARS_PER_PAGE = 2500          # a typical scanned page's text
CHARS_PER_TOKEN = 3.5
PROMPT_TOKENS = 1500           # system prompt + tool + details, per request
OUTPUT_PER_PAGE = 60           # ~one chunk (~120 tokens) per two pages


def estimate_chunking(file_ids, batch: bool = True) -> dict:
    """A rough price from the page counts docs.db already has (no PDFs opened)."""
    from server.services.Claude.claudeAgent import PRICES
    rows = files_by_uuid(file_ids)
    ok = [r for r in rows if not is_chunkable(r)]
    pages = sum(r.get("page_count") or 10 for r in ok)
    requests = sum(max(1, math.ceil((r.get("page_count") or 10) * CHARS_PER_PAGE / 200_000)) for r in ok)
    input_tokens = pages * CHARS_PER_PAGE / CHARS_PER_TOKEN + requests * PROMPT_TOKENS
    output_tokens = pages * OUTPUT_PER_PAGE + requests * 200
    price_in, price_out = PRICES.get(CHUNK_MODEL, PRICES["claude-opus-5-5"])
    usd = (input_tokens * price_in + output_tokens * price_out) / 1_000_000 * (0.5 if batch else 1)
    return {"files": len(ok), "not_chunkable": len(rows) - len(ok), "pages": pages, "requests": requests,
            "input_tokens": int(input_tokens), "output_tokens": int(output_tokens), "usd": round(usd, 2),
            "model": CHUNK_MODEL, "batch": batch}


# ---------- submit (Batch API) ----------

def submit_chunk_batch(file_ids, on_event=_no_events) -> dict:
    """Send these files' text to the Batch API for chunking. Files that aren't
    scanned PDFs, or are already waiting, are left out.
    on_event gets ("chunk_total", n), ("chunk_sent", sent, n) and
    ("chunk_file", file_id, status, detail).
    Returns {"batches": [...], "sent", "skipped", "failed"}."""
    from server.services.Claude.batchApi import _create_batches          # same upload (halves the batch on 413)
    with WORK_LOCK:
        rows = files_by_uuid(file_ids)
        jobs = load_jobs()
        todo, skipped, failed = [], 0, 0
        for row in rows:
            why = is_chunkable(row)
            if why or row["id"] in jobs["files"]:
                skipped += 1
                continue
            todo.append(row)
        print(f"Chunking {len(todo)} files with the Batch API" + (f" ({skipped} left out)" if skipped else ""))
        on_event("chunk_total", len(todo))
        created, sent = [], set()
        group, group_bytes = [], 0

        def flush():
            nonlocal group, group_bytes
            if not group:
                return
            for batch_id, reqs in _create_batches(group):
                ids = {_split_custom_id(r["custom_id"])[0] for r in reqs}
                if batch_id is None:
                    for fid in ids:
                        jobs["files"].pop(fid, None)
                        chunk_failed(fid, "a request was too large for the Batch API (use Chunk now)")
                    continue
                jobs["batches"][batch_id] = {"submitted_at": datetime.now().isoformat(timespec="seconds")}
                for r in reqs:
                    fid, part = _split_custom_id(r["custom_id"])
                    if fid in jobs["files"]:
                        jobs["files"][fid]["part_batches"][part] = batch_id
                save_jobs(jobs)                         # record first, so a crash can't lose the job
                set_chunk_status(ids, CHUNK_QUEUED, batch_id=batch_id)
                created.append(batch_id)
                sent.update(ids)
                for fid in ids:
                    on_event("chunk_file", fid, CHUNK_QUEUED, batch_id)
                on_event("chunk_sent", len(sent), len(todo))
                print(f"Submitted chunk batch {len(created)} ({batch_id}): {len(reqs)} requests, "
                      f"{len(sent)} of {len(todo)} files sent so far.")
            group, group_bytes = [], 0

        try:
            for n, row in enumerate(todo, 1):
                try:
                    pages, meta, parts = _prepare(row)
                    requests = [Request(custom_id=_custom_id(row["id"], i),
                                        params=build_request(row, meta, pages, first, last, len(pages)))
                                for i, (first, last) in enumerate(parts)]
                except Exception as e:
                    failed += 1
                    print(f"  cannot chunk {row['relpath']}: {e}")
                    chunk_failed(row["id"], str(e))
                    on_event("chunk_file", row["id"], "failed", str(e))
                    continue
                jobs["files"][row["id"]] = {"relpath": row["relpath"], "sha256": row.get("sha256"),
                                            "parts": [list(p) for p in parts],
                                            "part_batches": [None] * len(parts)}
                for r in requests:
                    size = len(json.dumps(r))
                    if group and (group_bytes + size > MAX_BATCH_BYTES or len(group) >= MAX_REQUESTS_PER_BATCH):
                        flush()
                    group.append(r)
                    group_bytes += size
                if n % 50 == 0:
                    print(f"  {n} of {len(todo)} files read")
            flush()
        except Exception as e:
            stuck = [fid for fid, info in jobs["files"].items() if None in info["part_batches"]]
            for fid in stuck:
                jobs["files"].pop(fid)
                chunk_failed(fid, f"chunk batch submission stopped: {e}")
            print(f"Chunk batch submission stopped: {e}")
            failed += len(stuck)
        finally:
            save_jobs(jobs)
        print(f"Chunking: {len(sent)} files sent in {len(created)} batches"
              + (f", {failed} could not be sent" if failed else "") + ".")
        return {"batches": created, "sent": len(sent), "skipped": skipped, "failed": failed}


# ---------- results ----------

def _error_text(result) -> str:
    if result.type == "errored":
        err = getattr(getattr(result, "error", None), "error", None)
        if err is not None:
            return f"batch request errored: {getattr(err, 'type', '')} {getattr(err, 'message', '')}".strip()
        return "batch request errored"
    return f"batch request {result.type}"


def _finish_file(file_id: str, info: dict, answers: list, usage: dict) -> int:
    """Cut and save one file's chunks from its parts' answers. Returns how many."""
    rows = files_by_uuid([file_id])
    if not rows:
        raise ValueError("no longer in docs.db")
    row = rows[0]
    if row["status"] != SCANNED:
        raise RuntimeError(f"file is {row['status']} now; chunk it again once it is scanned")
    if info.get("sha256") and row.get("sha256") and info["sha256"] != row["sha256"]:
        raise RuntimeError("file changed since it was sent; chunk it again")
    pages, meta, parts = _prepare(row)
    if [list(p) for p in parts] != info["parts"]:
        raise RuntimeError("file text changed since it was sent; chunk it again")
    chunks = rows_from_parts(pages, parts, answers)
    if not chunks:
        raise RuntimeError("no chunks with text")
    return save_chunks(file_id, chunks, source="chunker", model=usage.get("model") or CHUNK_MODEL,
                       version=CHUNKER_VERSION, doc=_doc(meta), cost=cost_usd(usage))


def apply_chunk_results(file_ids: list, jobs: dict, on_event=_no_events) -> tuple[int, int]:
    from server.services.Claude.claudeAgent import get_client
    client = get_client()
    wanted = set(file_ids)
    answers = defaultdict(dict)                         # file_id -> {part: chunks | error str}
    usage = defaultdict(lambda: empty_usage("batch"))
    batch_ids = {b for fid in wanted for b in jobs["files"][fid]["part_batches"]}
    for batch_id in sorted(batch_ids):
        for item in client.messages.batches.results(batch_id):
            if not is_chunk_request(item.custom_id):
                continue                                 # a scan request: batchApi handles those
            file_id, part = _split_custom_id(item.custom_id)
            if file_id not in wanted:
                continue
            if item.result.type == "succeeded":
                add_usage(usage[file_id], usage_of(item.result.message, "batch"))
                try:
                    answers[file_id][part] = extract_chunks(item.result.message, item.custom_id)
                except Exception as e:
                    answers[file_id][part] = str(e)
            else:
                answers[file_id][part] = _error_text(item.result)

    name = f"chunk_batch_api_{datetime.now():%Y-%m-%d_%H%M%S}"
    add_batch(name)
    set_message_batch_ids(name, batch_ids)
    done = failed = 0
    for file_id in file_ids:
        info = jobs["files"][file_id]
        got = answers.get(file_id, {})
        try:
            errors = [v for v in got.values() if isinstance(v, str)]
            missing = len(info["parts"]) - len(got)
            if errors or missing:
                raise RuntimeError("; ".join(errors) or f"{missing} part results missing")
            n = _finish_file(file_id, info, [got[i] for i in range(len(info["parts"]))], usage[file_id])
            record_run_usage(name, usage[file_id], files_done=1)
            done += 1
            print(f"chunked: {info['relpath']} ({n} chunks)")
            on_event("chunk_file", file_id, "chunked", n)
        except Exception as e:
            failed += 1
            print(f"chunking FAILED: {info['relpath']} - {e}")
            try:
                chunk_failed(file_id, str(e))
                record_run_usage(name, usage[file_id] if file_id in usage else None, files_failed=1)
            except Exception as e2:
                print("  could not record that in docs.db:", e2)
            on_event("chunk_file", file_id, "failed", str(e))
    set_batch_status(name, "done")
    return done, failed


def check_chunk_batches(verbose=False, on_event=_no_events) -> dict:
    """Check every recorded chunk batch once and save the chunks of files whose
    batches have all ended. Returns {"batches", "requests_done",
    "requests_total", "files_waiting", "applied"}."""
    jobs = load_jobs()
    progress = {"batches": 0, "requests_done": 0, "requests_total": 0, "files_waiting": 0, "applied": None}
    if not jobs["batches"]:
        if verbose:
            print("No chunk batches waiting.")
        return progress
    from server.services.Claude.claudeAgent import get_client
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
            print(f"Chunk batch {batch_id}: {batch.processing_status} "
                  f"({c.processing} processing, {c.succeeded} succeeded, {c.errored} errored)")

    ready = [fid for fid, info in jobs["files"].items() if set(info["part_batches"]) <= ended]
    if ready:
        print(f"\n{len(ready)} files have chunking results; saving their chunks...")
        done, failed = apply_chunk_results(ready, jobs, on_event)
        for fid in ready:
            jobs["files"].pop(fid)
        print(f"Chunking results: {done} chunked, {failed} failed")
        progress["applied"] = (done, failed)

    still_needed = {b for info in jobs["files"].values() for b in info["part_batches"]}
    for batch_id in ended - still_needed:
        jobs["batches"].pop(batch_id)
    save_jobs(jobs)
    progress["batches"] = len(jobs["batches"])
    progress["requests_done"] = sum(counts[b][0] for b in jobs["batches"] if b in counts)
    progress["requests_total"] = sum(counts[b][1] for b in jobs["batches"] if b in counts)
    progress["files_waiting"] = len(jobs["files"])
    return progress


# ---------- regular API, straight away ----------

def _chunk_one(row: dict) -> tuple[int, dict]:
    from server.services.Claude.claudeAgent import get_client
    pages, meta, parts = _prepare(row)
    usage, answers = empty_usage("regular"), []
    for i, (first, last) in enumerate(parts):
        params = build_request(row, meta, pages, first, last, len(pages))
        with get_client().messages.stream(**params) as stream:
            message = stream.get_final_message()
        add_usage(usage, usage_of(message, "regular"))
        answers.append(extract_chunks(message, f"{row['file']} part {i + 1}"))
    chunks = rows_from_parts(pages, parts, answers)
    if not chunks:
        raise RuntimeError("no chunks with text")
    n = save_chunks(row["id"], chunks, source="chunker", model=usage.get("model") or CHUNK_MODEL,
                    version=CHUNKER_VERSION, doc=_doc(meta), cost=cost_usd(usage))
    return n, usage


def chunk_now(file_ids, on_event=_no_events, workers: int = WORKERS) -> tuple[int, int]:
    """Chunk these files with the regular API now. Returns (chunked, failed)."""
    rows = [r for r in files_by_uuid(file_ids) if not is_chunkable(r)]
    if not rows:
        print("Nothing to chunk (only scanned PDFs that aren't waiting at the Batch API).")
        return 0, 0
    name = f"chunk_regular_{datetime.now():%Y-%m-%d_%H%M%S}"
    add_batch(name)
    run_usage, done, failed = empty_usage("regular"), 0, 0
    lock = threading.Lock()
    on_event("chunk_total", len(rows))
    print(f"Chunking {len(rows)} files with the regular API...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_chunk_one, r): r for r in rows}
        for fut in as_completed(futures):
            row = futures[fut]
            try:
                n, usage = fut.result()
                with lock:
                    done += 1
                    add_usage(run_usage, usage)
                record_run_usage(name, usage, files_done=1)
                print(f"chunked: {row['relpath']} ({n} chunks)")
                on_event("chunk_file", row["id"], "chunked", n)
            except Exception as e:
                failed += 1
                print(f"chunking FAILED: {row['relpath']} - {e}")
                chunk_failed(row["id"], str(e))
                record_run_usage(name, None, files_failed=1)
                on_event("chunk_file", row["id"], "failed", str(e))
            on_event("chunk_sent", done + failed, len(rows))
    set_batch_status(name, "done")
    if run_usage["requests"]:
        print(f"Chunking tokens: {run_usage['input_tokens']:,} in, {run_usage['output_tokens']:,} out,"
              f" about ${cost_usd(run_usage):.2f} (regular API)")
    return done, failed


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Search chunks for files that are already scanned.")
    ap.add_argument("--sharepoint", action="store_true", help="every scanned PDF on SharePoint not chunked yet")
    ap.add_argument("--include-chunked", action="store_true", help="with --sharepoint: redo chunked ones too")
    ap.add_argument("--file", action="append", default=[], help="a file's uuid (repeatable)")
    ap.add_argument("--limit", type=int, help="at most this many files")
    ap.add_argument("--now", action="store_true", help="regular API now instead of the Batch API")
    ap.add_argument("--check", action="store_true", help="check chunk batches once")
    ap.add_argument("--yes", action="store_true", help="don't ask before sending")
    args = ap.parse_args()
    from server.system.db.dbfunctions.database import init_db
    init_db()
    if args.check:
        with WORK_LOCK:
            print(check_chunk_batches(verbose=True))
        sys.exit()
    ids = list(args.file) + (sharepoint_ids(args.include_chunked) if args.sharepoint else [])
    if args.limit:
        ids = ids[:args.limit]
    if not ids:
        sys.exit("No files picked: use --sharepoint or --file <uuid>.")
    e = estimate_chunking(ids, batch=not args.now)
    print(f"{e['files']} files, ~{e['pages']} pages, {e['requests']} requests, about ${e['usd']:.2f} "
          f"({'Batch API' if e['batch'] else 'regular API'}, {e['model']}). A rough guess.")
    if not args.yes and input("Send? y/n: ").strip().lower() != "y":
        sys.exit()
    if args.now:
        print(chunk_now(ids))
    else:
        print(submit_chunk_batch(ids))
        print("Results are picked up by the app's batch poller, or run this again with --check.")
