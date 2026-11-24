#we manage jobs Here. 
# 
# 
# In a loop, program should
# 1: Look for failures and edge cases
#-Files that failed to download to sharepoint, anything that says pending on start, since that would mean the server restarded
# 2: Check the status of stuff like sharepoint again and claude batches, this also kind of ties into the last step
# 3: get new files and load them into the db
# Azure: run azure doc intel on scanned pdfs
# this should all be on a timer and pasue when large processes are happening the threee that will pause the main loop will be 
# 1: claude processes, once batches are sent, then we are good to only check the status in the loop
# 2: Sharepoint Uploads
# Doc intel: When OCR is running we pause 
# 

#job types 
#batching for claude
#sending to doc intel
#uploading to sharepoint

#apply claude
#apply doc intel

#add new files

from services.Claude.batchApi import check_batches
from system.db.dbfunctions.files import add_new_items
import time
from services.Azure.add_ocr import process_files
from services.SharePoint.sharepoint_manager import sync_to_sharepoint
from services.SharePoint.searchSharepoint import maybe_check_failed_uploads


#chunking is kidn of part of db syncing but it is a large very important process within this system so should keep it seperate

job_states = ["claude_batching", "sharepoint_sync", "claude_processing", "doc_intel", "db_syncing", "chunking"]


# def initial_sharepoint_sync():
    #this is more future tense for this project, it'd be nice but not the most significant thing to get this working considering how much work it will be
    #potentially rename syncing eventually
    #any files that have been modified in sharepoint or deleted. 
    #any files that are on sharepoint but not locally

    #initial sharepoint sync when server starts, too big of a query to run every cycle 

    #if some failed, there is usually 1 or 2 that are too large, see if they have been added manually, then get their id and put it in db

    #check and see if any files are processed and need to be added to sharepoint

def mainLoop():
    check_batches()
    sync_to_sharepoint()

    while True:
        #pretty similar to on start

        #check files and folders that need to be uploaded to sharepoint or that failed

        #check claude batches first thing, i think I already do this but then again it may be trying to do so on a seperate thread as is, so I will check the db to see if any are processing, if not continue, else check and see if they are ready, if not then, continue 
        check_batches()

        #add new items to the db
        add_new_items()

        #process files with doc intel
        process_files()

        sync_to_sharepoint()

        #once in a while (every few hours), look on SharePoint for failed uploads that were added by hand
        try:
            maybe_check_failed_uploads()
        except Exception as e:
            print(f"SharePoint: checking failed uploads didn't work this time - {e}")

        #upload to sharepoint
        time.sleep(30)





    #once all these steps are checked, then wait 30 more seconds
   


    








def onStart():

#the main loop will handle a lot of stuff this will be mostly to check corrupted processed then decide what to do with them on start, plus anything that needs to be initialized just oncethen this will call main loop

    mainLoop()

