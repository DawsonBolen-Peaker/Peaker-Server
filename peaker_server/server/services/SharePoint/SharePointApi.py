import asyncio
import os
from pathlib import Path

import requests
from azure.identity.aio import ClientSecretCredential
from msgraph import GraphServiceClient
from msgraph.generated.drives.item.items.item.create_upload_session.create_upload_session_post_request_body import (
    CreateUploadSessionPostRequestBody,
)
from msgraph.generated.models.drive_item import DriveItem
from msgraph.generated.models.folder import Folder
from system.db.dbfunctions.files import get_files_for_sharepoint

drive_id = os.environ["SHAREPOINT_DRIVE_ID"]
tenant_id = os.environ["SHAREPOINT_TENANT_ID"]
client_id = os.environ["SHAREPOINT_CLIENT_ID"]
client_secret = os.environ["SHAREPOINT_CLIENT_SECRET"]


SMALL_LIMIT = 4 * 1024 * 1024
CHUNK = 5 * 1024 * 1024 


async def upload_file(client, f: Path, parent_id: str = "root", name: str | None = None):
    """Upload f under parent_id, as name when given (a file outside Locomotive_Scans
    can have a different name on SharePoint than on disk), else as its own name."""
    data = f.read_bytes()
    size = len(data)
    path_item = f"{parent_id}:/{name or f.name}:"

    if size <= SMALL_LIMIT:
        item = await (
            client.drives.by_drive_id(drive_id)
            .items.by_drive_item_id(path_item)
            .content.put(data)
        )
        print(item.web_url)
        return item

    session = await (
        client.drives.by_drive_id(drive_id)
        .items.by_drive_item_id(path_item)
        .create_upload_session.post(CreateUploadSessionPostRequestBody())
    )
    url = session.upload_url
    last = None
    for start in range(0, size, CHUNK):
        chunk = data[start:start + CHUNK]
        last = requests.put(
            url,
            data=chunk,
            headers={
                "Content-Length": str(len(chunk)),
                "Content-Range": f"bytes {start}-{start + len(chunk) - 1}/{size}",
            },
        )
        last.raise_for_status()
    print(last.json().get("webUrl"))
    return last.json()


async def create_folder(client, folder_name: str, parent_id: str = "root"):
    folder = DriveItem(
        name=folder_name,
        folder=Folder(),
        additional_data={"@microsoft.graph.conflictBehavior": "rename"},
    )
    return await (
        client.drives.by_drive_id(drive_id)
        .items.by_drive_item_id(parent_id)
        .children.post(folder)
    )


async def postFileToSharepoint(file: Path, parent_id: str = "root"):
    credential = ClientSecretCredential(
        tenant_id, client_id, client_secret
    )
    client = GraphServiceClient(
        credential,
        scopes=["https://graph.microsoft.com/.default"]
    )
    try:
        item = await upload_file(client, file, parent_id)
        return item
    finally:
        await credential.close()


async def postFolderToSharepoint(
    name: str,
    parent_id: str = "root"
):
    credential = ClientSecretCredential(
        tenant_id, client_id, client_secret
    )
    client = GraphServiceClient(
        credential,
        scopes=["https://graph.microsoft.com/.default"]
    )
    try:
        folder = await create_folder(client, name, parent_id)
        return folder
    finally:
        await credential.close()


#this can still just happen after file upload but yeah
# (sharepoint_manager.sync_to_sharepoint does the real upload, with folders and name clashes;
#  get_files_for_sharepoint only returns scanned PDFs, other file types go up on request)
def upload_unadded_sharepoint():
    files = get_files_for_sharepoint()
    for file in files:
        print(f"waiting for SharePoint: {file['root_folder']}/{file['relpath']}")





#I have decided to work on this in a seperate workflow to test then i will implement here


#Here is a brief outline of functionality though


#check folders and files and use pd df to compare sharepoint with local. 
#there are too many potential edge cases if db is the only source of truth here


#work to make sure that evertything matches

#add any folders that contain processed files that are not present in sharepoint. 
# and then add them to db


#Any new untracked stuff can happen later on we do need to make the folder first in those cases 
#because the folder will need a sharepoint id to match it


#I would prefer to run the ai batch first then upload to sharepoint so it is 2 distinct workflows
#additionally i don't want one getting caught up on the other


