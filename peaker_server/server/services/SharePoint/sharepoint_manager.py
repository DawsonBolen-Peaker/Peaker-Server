
import asyncio, json, os, sys, threading
from pathlib import Path

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))     # server/

from system.db.dbfunctions.database import get_folder, sharepoint_owner
from system.db.dbfunctions.helpers import is_top_level
from system.db.dbfunctions.folders import (mark_folder_sharepoint, rename_folder_on_sharepoint,
                                           top_level_names_for_folder)
from system.db.dbfunctions.files import (mark_file_sharepoint, files_to_tag, set_file_tags, mark_file_tagged,
                                         rename_file_for_sharepoint, top_level_names_for,
                                         mark_sharepoint_failed, sharepoint_upload_name)
from system.db.dbfunctions.queries.queries import folders_not_on_sharepoint, files_not_on_sharepoint
from system.paths import OUTPUT_DIR, file_path

# Library columns the scan's tags go in (internal names; created as text
# columns the first time if the library doesn't have them). Departments are
# "; "-joined, e.g. "Engineering; Quality".
DEPARTMENT_COLUMN = "Department"
DOC_TYPE_COLUMN = "DocumentType"
# Off for now: the app registration gets "access denied" on the library's
# columns (it needs the Graph permission Sites.ReadWrite.All, plus
# Sites.Manage.All to create the columns). Set to True once that's granted.
TAGGING_ENABLED = False

REQUIRED_ENV = ("SHAREPOINT_DRIVE_ID", "SHAREPOINT_TENANT_ID", "SHAREPOINT_CLIENT_ID", "SHAREPOINT_CLIENT_SECRET")

# One sync at a time (the web view queues one more if asked during a sync).
_sync_lock = threading.Lock()


def _no_events(*args):
    pass


def is_configured() -> bool:
    return all(os.environ.get(k, "").strip() for k in REQUIRED_ENV)


def base_parent_id() -> str:
    return os.environ.get("SHAREPOINT_PARENT_ID", "").strip() or "root"


def _item_id(item):
    """Drive item id from a msgraph DriveItem or the JSON dict a chunked upload returns."""
    if isinstance(item, dict):
        return item.get("id")
    return getattr(item, "id", None)


async def _find_child(sp, client, parent_id: str, name: str):
    """The existing item called `name` directly under parent_id, or None."""
    try:
        return await (client.drives.by_drive_id(sp.drive_id)
                      .items.by_drive_item_id(f"{parent_id}:/{name}:").get())
    except Exception as e:
        if "404" in str(e) or "itemNotFound" in str(e) or "ResourceNotFound" in str(e):
            return None
        raise


def _item_name(item):
    if isinstance(item, dict):
        return item.get("name")
    return getattr(item, "name", None)


# How many times a top-level name clash is renamed around before giving up on
# that item for this sync.
MAX_RENAMES = 5


async def _top_level_folder(sp, client, row, parent_sp):
    """Find or create a top-level folder by its SharePoint name. Every root is
    flattened onto the same SharePoint folder, so a folder found there under
    that name may be another root's: then this one gets a new SharePoint name
    (docs.db only; nothing on disk changes). A folder there that no docs.db row
    owns is taken over, as for any folder (it is ours from an interrupted sync,
    or one made by hand that the documents belong in)."""
    folder_id = int(row["folder_id"])
    name = row["sharepoint_name"] or row["name"]
    for _ in range(MAX_RENAMES):
        if name.casefold() not in top_level_names_for_folder(folder_id):
            item = await _find_child(sp, client, parent_sp, name)
            if item is None:
                return await sp.create_folder(client, name, parent_sp)
            owner = sharepoint_owner(_item_id(item))
            if getattr(item, "folder", None) is not None and owner in (None, ("folder", folder_id)):
                return item
        # another row's name in docs.db, or something else on SharePoint (a file, or a folder of another row)
        new = rename_folder_on_sharepoint(folder_id, also_taken=name)
        print(f"  SharePoint: {name!r} is already taken there, so {row['root_folder']}/{row['relpath']}"
              f" goes up as {new!r}")
        name = new
    raise RuntimeError(f"still no free name after {MAX_RENAMES} tries")


async def _upload_folders(sp, client, on_event) -> tuple[int, int]:
    folders = folders_not_on_sharepoint()
    if folders.empty:
        return 0, 0
    # Parents before children: a folder's relpath always has fewer parts than its subfolders'.
    folders = folders.assign(depth=folders["relpath"].str.count("/")).sort_values(["depth", "relpath"])
    done = failed = 0
    for row in folders.to_dict("records"):
        if row["parent_id"] is None or row["parent_id"] != row["parent_id"]:      # None / NaN: top level
            parent_sp = base_parent_id()
        else:
            parent = get_folder(int(row["parent_id"]))
            parent_sp = parent and parent["uploaded_to_sharepoint"] and parent["sharepoint_id"]
        if not parent_sp:
            failed += 1
            print(f"  SharePoint: skipped folder {row['relpath']} (its parent isn't on SharePoint yet)")
            continue
        try:
            if is_top_level(row["relpath"]):
                item = await _top_level_folder(sp, client, row, parent_sp)
            else:
                # under a parent folder that is unique on SharePoint: its own name can't clash
                name = row["sharepoint_name"] or row["name"]
                item = await _find_child(sp, client, parent_sp, name)
                if item is None:
                    item = await sp.create_folder(client, name, parent_sp)
            # create_folder lets SharePoint add a number if the name was taken meanwhile; keep what it chose
            mark_folder_sharepoint(int(row["folder_id"]), _item_id(item), _item_name(item))
            done += 1
            print(f"  SharePoint: folder {row['root_folder']}/{row['relpath']}")
            on_event("sharepoint", "folder", row["relpath"])
        except Exception as e:
            failed += 1
            print(f"  SharePoint: FAILED folder {row['relpath']} - {e}")
    return done, failed


async def _free_top_level_path(sp, client, row, path, parent_sp):
    """A file directly inside a root lands in the SharePoint folder every root
    is flattened onto, where an upload replaces whatever has its name. If that
    name is another docs.db folder's or file's (there, or waiting to go up),
    give it a new name first: on disk for Locomotive_Scans files, on SharePoint
    only for files in the other roots (those are never renamed or moved).
    Returns (path, name to upload as)."""
    name = sharepoint_upload_name(row["id"]) or path.name
    for _ in range(MAX_RENAMES):
        clash = name.casefold() in top_level_names_for(row["id"])
        if not clash:
            item = await _find_child(sp, client, parent_sp, name)
            owner = item is not None and sharepoint_owner(_item_id(item))
            # no row owns it: our own earlier upload of this file (a rescan clears its id), so replace it
            clash = bool(owner) and owner != ("file", row["id"])
        if not clash:
            return path, name
        path = rename_file_for_sharepoint(row["id"], also_taken=name)
        name = sharepoint_upload_name(row["id"]) or path.name
    raise RuntimeError(f"still no free name after {MAX_RENAMES} tries")


async def _upload_files(sp, client, on_event) -> tuple[int, int]:
    files = files_not_on_sharepoint(scanned_only=True)
    done = failed = 0
    on_event("sharepoint_total", len(files))
    for n, row in enumerate(files.to_dict("records"), 1):
        path = file_path(row)
        try:
            if row["folder_id"] is None or row["folder_id"] != row["folder_id"]:
                parent_sp = base_parent_id()
            else:
                folder = get_folder(int(row["folder_id"]))
                parent_sp = folder and folder["uploaded_to_sharepoint"] and folder["sharepoint_id"]
                if not parent_sp:
                    raise RuntimeError("its folder isn't on SharePoint yet")
            if not path.is_file():
                raise FileNotFoundError(path)
            name = None
            if is_top_level(row["relpath"]):
                path, name = await _free_top_level_path(sp, client, row, path, parent_sp)
            else:
                name = sharepoint_upload_name(row["id"])
            item = await sp.upload_file(client, path, parent_sp, name=name)
            mark_file_sharepoint(row["id"], _item_id(item))
            done += 1
        except Exception as e:
            failed += 1
            print(f"  SharePoint: FAILED {row['relpath']} - {e}")
            # remembered, so searchSharepoint.check_failed_uploads() can find it if it's uploaded by hand
            try:
                mark_sharepoint_failed(row["id"], f"{type(e).__name__}: {e}")
            except Exception as e2:
                print(f"  could not record the failure in docs.db: {e2}")
        on_event("sharepoint_progress", n, len(files))
    return done, failed


async def _ensure_columns(sp, client) -> tuple[str, str]:
    """Make sure the drive's document library has the tag columns.
    Returns (site id, list id) of the library."""
    from msgraph.generated.models.column_definition import ColumnDefinition
    from msgraph.generated.models.text_column import TextColumn

    lst = await client.drives.by_drive_id(sp.drive_id).list_.get()     # list_: the SDK's name for /list
    site_id = lst.parent_reference.site_id
    columns = client.sites.by_site_id(site_id).lists.by_list_id(lst.id).columns
    existing = await columns.get()
    names = {n for c in existing.value or [] for n in (c.name, c.display_name) if n}
    for name in (DEPARTMENT_COLUMN, DOC_TYPE_COLUMN):
        if name not in names:
            await columns.post(ColumnDefinition(name=name, display_name=name, text=TextColumn()))
            print(f"  SharePoint: added column {name} to the library")
    return site_id, lst.id


def _sidecar_index() -> dict:
    """{uuid: sidecar path} for every scan record in the output folder."""
    try:
        return {p.stem: p for p in OUTPUT_DIR.glob("*/sidecars/*.json")}
    except OSError:
        return {}


async def _tag_files(sp, client) -> tuple[int, int]:
    """Set Department and DocumentType on uploaded files that don't have them yet."""
    from msgraph.generated.models.field_value_set import FieldValueSet

    files = files_to_tag()
    if files.empty:
        return 0, 0
    try:
        site_id, list_id = await _ensure_columns(sp, client)
    except Exception as e:
        print(f"  SharePoint: could not check the Department/DocumentType columns, not tagging - {e}")
        return 0, len(files)
    sidecars = None
    done = failed = 0
    for row in files.to_dict("records"):
        doc_type, departments = row["doc_type"], row["departments"]
        if not doc_type and not departments:
            # scanned before docs.db kept tags: take them from the scan's sidecar
            sidecars = _sidecar_index() if sidecars is None else sidecars
            path = sidecars.get(row["id"])
            if path is None:
                continue                    # nothing known to tag it with
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            set_file_tags(row["id"], record.get("doc_type"), record.get("departments"))
            doc_type = record.get("doc_type") or ""
            departments = "; ".join(d for d in record.get("departments") or [] if isinstance(d, str))
        try:
            # a file's columns live on its list item in the library
            item = await (client.drives.by_drive_id(sp.drive_id).items
                          .by_drive_item_id(row["sharepoint_id"]).list_item.get())
            await (client.sites.by_site_id(site_id).lists.by_list_id(list_id).items
                   .by_list_item_id(item.id).fields.patch(FieldValueSet(additional_data={
                       DEPARTMENT_COLUMN: departments, DOC_TYPE_COLUMN: doc_type})))
            mark_file_tagged(row["id"])
            done += 1
        except Exception as e:
            failed += 1
            print(f"  SharePoint: could not tag {row['relpath']} - {e}")
    return done, failed


async def _sync(on_event) -> dict:
    from services.SharePoint import SharePointApi as sp       # reads the SHAREPOINT_* settings on import
    from azure.identity.aio import ClientSecretCredential
    from msgraph import GraphServiceClient

    credential = ClientSecretCredential(sp.tenant_id, sp.client_id, sp.client_secret)
    client = GraphServiceClient(credential, scopes=["https://graph.microsoft.com/.default"])
    try:
        folders_done, folders_failed = await _upload_folders(sp, client, on_event)
        files_done, files_failed = await _upload_files(sp, client, on_event)
        tagged, tag_failed = await _tag_files(sp, client) if TAGGING_ENABLED else (0, 0)
    finally:
        await credential.close()
    return {"folders": folders_done, "files": files_done, "failed": folders_failed + files_failed,
            "tagged": tagged, "tag_failed": tag_failed}



def sync_to_sharepoint(on_event=_no_events):
    """Upload every folder and scanned PDF that isn't on SharePoint yet (other
    file types are only tracked; they go up on request).
    Returns counts, or None when SharePoint isn't configured or a sync is already running."""
    if not is_configured():
        print("SharePoint isn't set up in .env (SHAREPOINT_*); skipping upload.")
        return None
    if not _sync_lock.acquire(blocking=False):
        return None
    try:
        print("SharePoint: checking for processed files that aren't uploaded yet...")
        result = asyncio.run(_sync(on_event))
        print(f"SharePoint: {result['folders']} folders and {result['files']} files uploaded"
              + (f", {result['failed']} failed (they are retried next time)" if result["failed"] else ""))
        if result["tagged"] or result["tag_failed"]:
            print(f"SharePoint: tagged {result['tagged']} files with department and document type"
                  + (f", {result['tag_failed']} not tagged (retried next time)" if result["tag_failed"] else ""))
        return result
    finally:
        _sync_lock.release()


if __name__ == "__main__":
    sync_to_sharepoint()
