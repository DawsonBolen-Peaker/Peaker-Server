import csv
import os
import threading

import anthropic
from pathlib import Path
import sys




try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass  # fine if the key is already in your environment



sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LOG_FILE = "claude_usage_log.csv"
COLUMNS = ["timestamp", "job_id", "run_type", "model", "total_pages",
           "image_pages", "failed_pages", "input_tokens", "output_tokens", "notes"]

_client = None
_lock = threading.Lock()


def get_client() -> anthropic.Anthropic:
    """Create the client on first use, so the app can start without a key."""
    global _client
    with _lock:
        if _client is None:
            key = os.environ.get("API_KEY") or os.environ.get("ANTHROPIC_API_KEY")
            if not key:
                raise RuntimeError("No API key: set API_KEY in .env")
            _client = anthropic.Anthropic(api_key=key, max_retries=5)
        return _client


def get_batch_usage(batch_id: str) -> dict:
    """Add up token usage across every result in one finished batch."""
    client = get_client()
    totals = {"model": "", "pages": 0, "failed": 0, "input": 0, "output": 0}

    for item in client.messages.batches.results(batch_id):
        totals["pages"] += 1
        if item.result.type == "succeeded":
            msg = item.result.message
            totals["model"] = totals["model"] or msg.model
            totals["input"] += msg.usage.input_tokens
            totals["output"] += msg.usage.output_tokens
        else:
            totals["failed"] += 1
    return totals


def already_logged() -> set:
    if not os.path.exists(LOG_FILE):
        return set()
    with open(LOG_FILE, newline="") as f:
        return {row["job_id"] for row in csv.DictReader(f)}


def backfill_batches():
    """Pull usage for every finished batch still within the 29-day window."""
    client = get_client()
    done = already_logged()
    new_file = not os.path.exists(LOG_FILE)

    with open(LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(COLUMNS)

        for b in client.messages.batches.list(limit=100):  # pages automatically
            if b.processing_status != "ended" or b.id in done:
                continue
            u = get_batch_usage(b.id)
            w.writerow([
                b.created_at.isoformat(timespec="seconds"), b.id, "batch",
                u["model"], "", u["pages"], u["failed"],
                u["input"], u["output"], "backfilled from API",
            ])
            print(f"{b.id}: {u['pages']} pages, "
                  f"{u['input']:,} in / {u['output']:,} out")


if __name__ == "__main__":
    backfill_batches()