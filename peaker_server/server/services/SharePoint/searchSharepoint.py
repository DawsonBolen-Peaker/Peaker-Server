"""Find failed SharePoint uploads that were uploaded by hand.

sync_to_sharepoint() records a failed upload on the file's row (files.sharepoint_error).
Some of those (usually one or two very large files) get uploaded to SharePoint by
hand. check_failed_uploads() looks each one up on SharePoint by name, confirms it
is the same document (same bytes, or the same text on its first three pages), and
then records its SharePoint id in docs.db like a normal upload.

Only failed uploads are checked, a few at a time, at most every CHECK_EVERY_HOURS
(the main loop calls maybe_check_failed_uploads() each cycle).

    python services/SharePoint/searchSharepoint.py          # check now
"""
import hashlib, os, sys
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))     # server/

import requests

from system.paths import file_path
from system.db.dbfunctions.database import get_meta, set_meta, sharepoint_owner
from system.db.dbfunctions.files import failed_sharepoint_uploads, mark_file_sharepoint, mark_sharepoint_checked
from system.db.dbfunctions.helpers import now

GRAPH = "https://graph.microsoft.com/v1.0"
CHECK_EVERY_HOURS = 6
FILES_PER_CHECK = 25
PAGES_TO_COMPARE = 3
TEXT_MATCH = 0.95                   # how alike the first pages' text must be
MAX_DOWNLOAD = 300 * 1024 * 1024    # bigger than this: name and size must match instead
LAST_CHECK_KEY = "sharepoint_failed_check_at"


def _settings():
    keys = ("SHAREPOINT_DRIVE_ID", "SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET")
    values = [os.environ.get(k, "").strip() for k in keys]
    return values if all(values) else None


def _token(tenant_id, client_id, client_secret) -> str:
    from azure.identity import ClientSecretCredential
    cred = ClientSecretCredential(tenant_id, client_id, client_secret)
    try:
        return cred.get_token("https://graph.microsoft.com/.default").token
    finally:
        cred.close()


def _search(session, drive_id: str, text: str) -> list[dict]:
    """Drive items whose name or content matches text (Graph search)."""
    q = text.replace("'", "''")
    url = f"{GRAPH}/drives/{drive_id}/root/search(q='{q}')"
    items = []
    while url and len(items) < 200:
        r = session.get(url, timeout=60)
        r.raise_for_status()
        data = r.json()
        items += data.get("value", [])
        url = data.get("@odata.nextLink")
    return [i for i in items if "file" in i]


def _download(session, drive_id: str, item_id: str) -> bytes:
    r = session.get(f"{GRAPH}/drives/{drive_id}/items/{item_id}/content", timeout=600)
    r.raise_for_status()
    return r.content


def _norm(text: str) -> str:
    return " ".join((text or "").split()).casefold()


def first_pages_text(data_or_path) -> str:
    """Text of the first few pages of a PDF (bytes or a path), whitespace and case normalized."""
    import pymupdf
    doc = (pymupdf.open(stream=data_or_path, filetype="pdf") if isinstance(data_or_path, (bytes, bytearray))
           else pymupdf.open(data_or_path))
    with doc:
        return _norm(" ".join(doc[i].get_text() for i in range(min(PAGES_TO_COMPARE, doc.page_count))))


def same_document(local: Path, data: bytes) -> bool:
    """True when SharePoint's copy is this file: the same bytes, or (for a PDF) the
    same text on its first pages, since a copy uploaded by hand may have been saved again."""
    if hashlib.sha256(data).hexdigest() == hashlib.sha256(local.read_bytes()).hexdigest():
        return True
    if local.suffix.lower() != ".pdf":
        return False
    try:
        a, b = first_pages_text(local), first_pages_text(data)
    except Exception:
        return False
    if not a or not b:
        return False            # image-only pages: nothing to compare but the bytes
    return a == b or SequenceMatcher(None, a[:5000], b[:5000]).ratio() >= TEXT_MATCH


def _candidates(row: dict) -> list[str]:
    """Names the file could have on SharePoint: its SharePoint name, its own name."""
    names = [row.get("sharepoint_name"), row["current_name"]]
    return list(dict.fromkeys(n for n in names if n))


def _find_on_sharepoint(session, drive_id: str, row: dict):
    """The id of the SharePoint item that is this file, or None."""
    local = file_path(row)
    if not local.is_file():
        return None
    size = local.stat().st_size
    names = {n.casefold() for n in _candidates(row)}
    stems = {Path(n).stem.casefold() for n in names}
    seen = set()
    for name in _candidates(row):
        for item in _search(session, drive_id, Path(name).stem):
            if item["id"] in seen:
                continue
            seen.add(item["id"])
            item_name = item.get("name", "")
            # text match on the name first: the same name, or the same name SharePoint numbered ("x 1.pdf")
            if item_name.casefold() not in names and not any(
                    Path(item_name).stem.casefold().startswith(s) for s in stems):
                continue
            if Path(item_name).suffix.casefold() != local.suffix.casefold():
                continue
            owner = sharepoint_owner(item["id"])
            if owner is not None and owner != ("file", row["id"]):
                continue                    # another docs.db row's file
            if item.get("size", 0) > MAX_DOWNLOAD:
                if item.get("size") == size and item_name.casefold() in names:
                    return item["id"]
                continue
            if same_document(local, _download(session, drive_id, item["id"])):
                return item["id"]
    return None


def check_failed_uploads(limit: int = FILES_PER_CHECK) -> dict | None:
    """Look for failed uploads on SharePoint and record the ones found.
    Returns {"checked", "found"}, or None when SharePoint isn't set up."""
    settings = _settings()
    if settings is None:
        return None
    rows = failed_sharepoint_uploads()[:limit]
    if not rows:
        return {"checked": 0, "found": 0}
    drive_id, tenant_id, client_id, client_secret = settings
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {_token(tenant_id, client_id, client_secret)}"
    found = 0
    for row in rows:
        try:
            item_id = _find_on_sharepoint(session, drive_id, row)
            if item_id:
                mark_file_sharepoint(row["id"], item_id)
                found += 1
                print(f"SharePoint: {row['root_folder']}/{row['relpath']} was uploaded by hand; recorded it")
        except Exception as e:
            print(f"SharePoint: could not look for {row['root_folder']}/{row['relpath']} - {e}")
        finally:
            mark_sharepoint_checked(row["id"])
    if rows:
        print(f"SharePoint: checked {len(rows)} failed uploads, {found} found on SharePoint")
    return {"checked": len(rows), "found": found}


def maybe_check_failed_uploads() -> dict | None:
    """check_failed_uploads(), at most every CHECK_EVERY_HOURS (remembered in docs.db)."""
    last = get_meta(LAST_CHECK_KEY)
    try:
        if last and datetime.now() - datetime.fromisoformat(last) < timedelta(hours=CHECK_EVERY_HOURS):
            return None
    except ValueError:
        pass
    result = check_failed_uploads()
    set_meta(LAST_CHECK_KEY, now())
    return result


if __name__ == "__main__":
    print(check_failed_uploads())
