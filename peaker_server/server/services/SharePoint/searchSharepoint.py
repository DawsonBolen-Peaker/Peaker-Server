import os
import pymupdf

#sync to sharepoint is an ok function in principal but some files fail and idk if they are too large or the timeout we set is too small, but it wouldn't be a terrible idea to literally just search by title on sharepoint or sharepoint name, since now we can technically have them be different 

#then literally just compare the strings of the first three pages, confirm they are the same file and then do what needs to be done,

#for if i ever decide to upload a failed one manually
#we don't have to do this for all of them just failed ones

drive_id = os.environ["SHAREPOINT_DRIVE_ID"]
tenant_id = os.environ["SHAREPOINT_TENANT_ID"]
client_id = os.environ["SHAREPOINT_CLIENT_ID"]
client_secret = os.environ["SHAREPOINT_CLIENT_SECRET"]


