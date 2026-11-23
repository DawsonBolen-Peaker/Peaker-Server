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


#chunking is kidn of part of db syncing but it is a large very important process within this system so should keep it seperate

job_states = ["claude_batching", "sharepoint_sync", "claude_processing", "doc_intel", "db_syncing", "chunking"]

def mainLoop():
    #pretty similar to on start

    #check files and folders that need to be uploaded to sharepoint or that failed

    #check claude batches
    check_batches()

    #no need to rapid fire, we will check all conditions once everything has been checked
    add_new_items()

    #once all these steps are checked, then wait 30 more seconds
    time.sleep(30)


    





    print('loop')


def onStart():

#the main loop will handle a lot of stuff this will be mostly to check corrupted processed then decide what to do with them on start, plus anything that needs to be initialized just oncethen this will call main loop

    mainLoop()

