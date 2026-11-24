import importlib.util, multiprocessing, sys
from system.db.dbfunctions.database import WORK_LOCK, UNSCANNED, IN_PROGRESS, init_db
from system.db.dbfunctions.files import add_new_items, files_with_status
from system.editConfigFiles import ensure_config
from system.paths import SERVER_DIR
from workers.processing.localScan import preview
from services.Claude.batchApi import BatchPoller, check_batches
from workers.processing.runner import run_all, uses_batch, BATCH_THRESHOLD, estimate
from services.SharePoint.sharepoint_manager import sync_to_sharepoint

import uvicorn

PREVIEW_LIMIT = 50  

def startup():
    init_db()
    ensure_config()


#this file will just be used to start the main loop which looks for new files then will add ocr through doc intell. I will handle the bulk of the new files through my other project since doc intel on 1000 documents would take for ever, but for future reference this will happen when a document is added. 
#This will also be used to start the fast api server to listen for requests


if __name__ == "__main__":
    multiprocessing.freeze_support()  
    startup()
    uvicorn.run("services.api.api:app", host="127.0.0.1", port=8000, log_level="info")




       

      


