"""Full-text search over the search chunks (chunks_fts in docs.db).

    python db/search.py "fuel injector 16-645E3"
    python db/search.py "catalytic converter" --type drawing --limit 5

search() ranks with bm25, weighting section titles and keywords above body
text, and returns the file, pages and a highlighted snippet for each hit.
A starting point for the search work; the UI doesn't use it yet.
"""
import sys
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))     # server/

import json, re
from server.system.db.dbfunctions.database import connect

# chunks_fts columns: text, section_title, summary, keywords, doc_title, doc_keywords
WEIGHTS = (1.0, 4.0, 2.0, 3.0, 2.0, 1.0)


def fts_query(text: str, any_word: bool = False) -> str:
    """User text -> an FTS5 query: each word quoted (so - . / in part numbers
    can't break the syntax), all required unless any_word."""
    words = re.findall(r'[^\s"]+', text or "")
    return (" OR " if any_word else " ").join(f'"{w}"' for w in words)


def search(text: str, limit: int = 20, chunk_type: str | None = None, any_word: bool = False) -> list[dict]:
    """Best chunks for text. Falls back to matching any word when every word finds nothing."""
    query = fts_query(text, any_word)
    if not query:
        return []
    where, params = ["chunks_fts MATCH ?"], [query]
    if chunk_type:
        where.append("c.chunk_type = ?")
        params.append(chunk_type)
    sql = f"""
        SELECT f.uuid AS file_id, f.root_folder, f.relpath, f.current_name AS file, f.doc_type,
               c.chunk_uuid, c.chunk_index, c.page_start, c.page_end, c.section_title, c.summary,
               c.keywords, c.chunk_type,
               snippet(chunks_fts, 0, '[', ']', ' ... ', 24) AS snippet,
               bm25(chunks_fts, {', '.join(map(str, WEIGHTS))}) AS score
        FROM chunks_fts
        JOIN chunks c ON c.id = chunks_fts.rowid
        JOIN files f ON f.file_id = c.file_id
        WHERE {' AND '.join(where)}
        ORDER BY score LIMIT ?"""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(sql, params + [limit])]
    if not rows and not any_word and len(query.split()) > 1:
        return search(text, limit, chunk_type, any_word=True)
    for r in rows:
        r["keywords"] = json.loads(r["keywords"] or "[]")
    return rows


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("query")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--type", help="only this chunk_type")
    a = ap.parse_args()
    for r in search(a.query, a.limit, a.type):
        pages = f"p{r['page_start']}" + (f"-{r['page_end']}" if r["page_end"] != r["page_start"] else "")
        print(f"{r['score']:.2f}  {r['root_folder']}/{r['relpath']}  {pages}  [{r['chunk_type']}] {r['section_title']}")
        print(f"       {r['snippet']}\n")
