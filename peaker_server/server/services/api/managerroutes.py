"""Routes of the scan manager app (scan-manager/document-server/routes/app.py),
served here under /api/manager. The React client in scan-manager reads these.

/files/{file_id}/content isn't repeated here: api.py already has that exact route.
"""
from fastapi import APIRouter, HTTPException, Query

from system.db.dbfunctions.database import (UNSCANNED, SCANNED, IN_PROGRESS, FAILED, DUPLICATE, SKIPPED,
                                            status_counts)
from system.db.dbfunctions.queries.queries import (list_file_listings, file_listing, list_folders,
                                                   sharepoint_counts)

STATUSES = (UNSCANNED, SCANNED, IN_PROGRESS, FAILED, DUPLICATE, SKIPPED)

router = APIRouter(prefix="/api/manager", tags=["manager"])


@router.get("/files")
def get_files(status: str | None = Query(None, description=f"one of: {', '.join(STATUSES)}"),
              folder: str | None = Query(None, description="folder relpath, \"\" for the root"),
              on_sharepoint: bool | None = None,
              search: str | None = Query(None, description="part of the file name"),
              limit: int | None = Query(None, ge=1, description="leave out for every file"),
              offset: int = Query(0, ge=0),
              root: str | None = Query(None, description="root folder name, e.g. Locomotive_Scans")):
    """Files on the network folder with their processing and SharePoint status."""
    if status is not None and status not in STATUSES:
        raise HTTPException(400, f"Unknown status {status!r}; use one of {list(STATUSES)}")
    return list_file_listings(status, folder, on_sharepoint, search, limit, offset, root_folder=root)


@router.get("/files/{file_id}")
def get_file(file_id: str):
    """One file by its uuid."""
    row = file_listing(file_id)
    if row is None:
        raise HTTPException(404, f"No file with id {file_id}")
    return row


@router.get("/folders")
def get_folders():
    """Folders on the network folder (every root) with their SharePoint status."""
    return list_folders()


@router.get("/status")
def get_status():
    """Counts for a dashboard: files per processing status, and SharePoint progress."""
    return {"files": status_counts(), "sharepoint": sharepoint_counts()}
