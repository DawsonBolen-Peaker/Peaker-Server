#I barely want to use this ever, so the goal is to just split documents into multiple if needed to send to batch, this is way more pricey and makes price harder to predict, not to mention it starts like 4 different threads
import shutil, tempfile
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from pathlib import Path

from server.services.Claude.claudeAgent import (build_context, plan_chunks, count_chunks, scan_chunk, merge_chunk_results,
                             pdf_page_count, ScanError)
from server.system.db.dbfunctions.database import (WORK_LOCK, UNSCANNED, add_new_items, files_with_status,
                         mark_processed, mark_failed, record_failed_usage)
from server.services.finance.usage import empty_usage, add_usage, cost_usd
from system.editConfigFiles import track_new_categories
from server.workers.processing.processRequest import process_document
from system.batchconfig import start_batch, finish_batch
from system.paths import file_path
from server.services.Claude.claude_chunk import cut_scan_chunks, save_scan_chunks

WORKERS = 4


def _no_events(*args):
    pass


def run_local(limit=None, workers=WORKERS, file_ids=None, on_event=_no_events):
    """Scan unscanned files (or just file_ids) with the regular API.

    on_event gets progress for a UI:
      ("total", chunk_count)               once, before work starts
      ("chunk", chunks_done, chunk_count)  after every chunk (or skipped chunk)
      ("file", file_id, status, detail)    when a file is scanned or fails
    """
    with WORK_LOCK:
        add_new_items()
        pending = files_with_status(UNSCANNED)
        if file_ids is not None:
            pending = pending[pending["id"].isin(set(file_ids))]
        if limit:
            pending = pending.head(limit)
        if pending.empty:
            print("Nothing to scan.")
            return

        ctx = build_context()
        name, archive_dir, out_dir = start_batch()
        temp_root = Path(tempfile.mkdtemp(prefix="file_scanner_"))
        executor = ThreadPoolExecutor(max_workers=workers)
        rows = iter(pending.to_dict("records"))
        docs = {}        # file_id -> {"row", "total", "chunks", "results", "usage"}
        run_usage = empty_usage("regular")      # everything this run was billed for
        in_flight = {}   # future -> (file_id, chunk_index)
        done, failed = 0, 0
        chunk_total = sum(_safe_count(file_path(r)) for r in pending.to_dict("records"))
        chunks_done = 0
        on_event("total", chunk_total)

        def advance(n=1):
            nonlocal chunks_done
            chunks_done = min(chunk_total, chunks_done + n)
            on_event("chunk", chunks_done, chunk_total)

        def fail(file_id, row, error):
            nonlocal failed
            failed += 1
            print("FAILED:", row["relpath"], "-", error)
            on_event("file", file_id, "failed", str(error))
            doc = docs.pop(file_id, None)
            try:
                mark_failed(file_id, str(error))
                record_failed_usage(name, doc["usage"] if doc else None)
            except Exception as e:
                print("  could not record the failure in docs.db:", e)
            if doc:
                for fut, (fid, _) in in_flight.items():
                    if fid == file_id:
                        fut.cancel()
                advance(len(doc["chunks"]) - len(doc["results"]))
            else:
                advance(_safe_count(file_path(row)))

        def feed():
            # Keep every thread busy, but don't split files far ahead of need.
            while len(in_flight) < workers * 2:
                row = next(rows, None)
                if row is None:
                    return
                file_id = row["id"]
                try:
                    path = file_path(row)
                    if not path.is_file():
                        raise FileNotFoundError(f"File not found: {path}")
                    chunks = plan_chunks(path, temp_root / file_id)
                    total = pdf_page_count(path)
                except Exception as e:
                    fail(file_id, row, e)
                    continue
                docs[file_id] = {"row": row, "total": total, "chunks": chunks, "results": {},
                                 "usage": empty_usage("regular")}
                if len(chunks) > 1:
                    print(f"{row['relpath']}: {len(chunks)} chunks")
                for i, (chunk_path, start, end) in enumerate(chunks):
                    fut = executor.submit(scan_chunk, ctx, chunk_path, row, start, end, total)
                    in_flight[fut] = (file_id, i)

        def finalize(file_id):
            nonlocal done
            doc = docs.pop(file_id)
            row = doc["row"]
            try:
                results = [(doc["results"][i], s, e) for i, (_, s, e) in enumerate(doc["chunks"])]
                result = merge_chunk_results(results)
                info = process_document(file_id, row["relpath"], result, ctx.folders, archive_dir, out_dir,
                                        root_folder=row["root_folder"])
                chunks = cut_scan_chunks(file_id, result, info["pages_removed"])   # before its pages are cleared
                # the file is rebuilt and moved now, so record it before anything else can fail
                mark_processed(file_id, info, name, usage=doc["usage"])
                save_scan_chunks(file_id, chunks, result)
                try:
                    track_new_categories(result.get("categories", []) + result.get("new_categories", []))
                except Exception as e:
                    print("  could not save new categories:", e)
                done += 1
                print("done:", info["relpath"])
                on_event("file", file_id, "scanned", info["relpath"])
            except Exception as e:
                docs[file_id] = dict(doc, results=doc["chunks"])   # all chunks already counted
                fail(file_id, row, e)
            finally:
                shutil.rmtree(temp_root / file_id, ignore_errors=True)

        try:
            feed()
            while in_flight:
                finished, _ = wait(list(in_flight), return_when=FIRST_COMPLETED)
                for fut in finished:
                    file_id, i = in_flight.pop(fut)
                    doc = docs.get(file_id)
                    if doc is None or fut.cancelled():
                        continue                       # file already failed
                    try:
                        record = fut.result()
                    except Exception as e:
                        if isinstance(e, ScanError):            # answered but unusable: still billed
                            add_usage(doc["usage"], e.usage)
                            add_usage(run_usage, e.usage)
                        fail(file_id, doc["row"], e)
                        continue
                    add_usage(doc["usage"], record.get("_usage"))
                    add_usage(run_usage, record.pop("_usage", None))
                    doc["results"][i] = record
                    advance()
                    if len(doc["results"]) == len(doc["chunks"]):
                        finalize(file_id)
                feed()
        except BaseException as e:
            # A big error (or Ctrl+C): stop here, but still archive what finished.
            print(f"\nRun stopped early: {e!r}")
            if not isinstance(e, Exception):
                raise
        finally:
            executor.shutdown(wait=False, cancel_futures=True)
            finish_batch(name, archive_dir, out_dir)
            shutil.rmtree(temp_root, ignore_errors=True)
            left = len(pending) - done - failed
            print(f"{done} processed, {failed} failed" + (f", {left} not reached" if left > 0 else ""))
            if run_usage["requests"]:
                print(f"Tokens: {run_usage['input_tokens']:,} in, {run_usage['output_tokens']:,} out,"
                      f" {run_usage['cache_read_tokens']:,} cache read, {run_usage['cache_write_tokens']:,}"
                      f" cache write, about ${cost_usd(run_usage):.2f} (regular API)")


def _safe_count(path) -> int:
    try:
        return count_chunks(path)
    except Exception:
        return 1


def preview(pending) -> None:
    """Print how many chunks each pending file needs."""
    for row in pending.to_dict("records"):
        try:
            n = count_chunks(file_path(row))
            print(f"  {row['relpath']}" + (f"  ({n} chunks)" if n > 1 else ""))
        except Exception:
            print(f"  {row['relpath']}  (unreadable)")
