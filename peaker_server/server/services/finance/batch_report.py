"""What each run sent to Claude and what it cost. From the Doc-Server folder:

    python server/finance/batch_report.py            # every run
    python server/finance/batch_report.py --csv      # also save batch_costs.csv next to the server folder

Token counts are what the API reported for each request; cost_usd is worked out
from them with the list prices in api/claudeAgent.PRICES (see finance/usage.py).
Runs from before this tracking was added show zeros.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))     # server/, so db / system import

from server.system.db.dbfunctions.database import batch_costs, connect

COLUMNS = ["name", "api", "files_done", "files_failed", "total_pages", "image_pages", "requests",
           "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "cost_usd"]


def main():
    df = batch_costs()
    tracked = df[df["requests"] > 0]
    if tracked.empty:
        print("No runs with usage recorded yet.")
    else:
        import pandas as pd
        with pd.option_context("display.max_columns", None, "display.width", 200):
            print(tracked[COLUMNS].to_string(index=False))
        t = tracked[COLUMNS[2:]].sum()
        print(f"\nAll runs: {int(t['files_done'])} files, {int(t['total_pages'])} pages "
              f"({int(t['image_pages'])} as images), {int(t['input_tokens']):,} in / "
              f"{int(t['output_tokens']):,} out tokens, about ${t['cost_usd']:.2f}")
    with connect() as conn:
        r = conn.execute("SELECT COUNT(*) AS n, COALESCE(SUM(page_count), 0) AS pages FROM files"
                         " WHERE status = 'scanned'").fetchone()
    print(f"Scanned files in docs.db: {r['n']}, {r['pages']} pages")
    if "--csv" in sys.argv:
        out = Path(__file__).resolve().parents[2] / "batch_costs.csv"
        df.to_csv(out, index=False)
        print("Saved", out)


if __name__ == "__main__":
    main()
