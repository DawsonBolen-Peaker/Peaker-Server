#in this file i will

#1: make a function that gets the cost of each batch. 

import pandas as pd
from pathlib import Path

current_dir = Path(__file__).resolve().parent
csv_path = current_dir.parent.parent / "claude_usage_log.csv"

# def loadPricingDataBatch():
#     df = pd.read_csv(csv_path)
#     for index, row in df.iterrows():


#the page images in the csv has nothing to do with the number of pages sent as images apparantly. maybe it's
#the number of docs i am not sure though



def getPricing(numpages, pagesscanned):
    #Here we basically can estimate the pricing of a batch based on total pages
    #Here is the breakdown
    #number of total pages / 1000 X 1.50 for doc intel
    #number of pages - number of pages that need to be processed by claude X whatever the rate is
    #number of pages that need processing X our current pricing
    
    #I would rather store the number in a text for like overall pages but then also just store the number of pages per document in the db
    
    #one important consideration when a blank page gets removed, probably by doc intel at this point or by claude, then we need to update the file
    print("hello")
