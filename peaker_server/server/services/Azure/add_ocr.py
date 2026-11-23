#this will be my new workflow for add ocr with doc intel
#we are not incorporating yet, only once we actually get doc intel, just somewhat setting it up for now

import os
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
import csv
from pathlib import Path

client = DocumentIntelligenceClient(
    endpoint=os.environ["DOCUMENTINTELLIGENCE_ENDPOINT"],
    credential=AzureKeyCredential(os.environ["DOCUMENTINTELLIGENCE_API_KEY"]),
)

current_dir = Path(__file__).resolve().parent
csv_dir = current_dir / "tempconfidencescores" 

def get_page_confidence_scores(result):
    rows = []
    for page in result.pages:
        scores = [w.confidence for w in (page.words or [])]
        rows = []
        if scores:
            avg = sum(scores) / len(scores)
            low = sum(1 for s in scores if s < 0.80)
            rows.append({
            "page": page.page_number,
            "word_count": len(scores),
            "avg_confidence": round(avg, 4),
            "min_confidence": round(min(scores), 4),
            "low_conf_words": low,
            "low_conf_pct": round(low / len(scores), 4),
            })
        else:
            rows.append({
                "page": page.page_number, "word_count": 0,
                "avg_confidence": None, "min_confidence": None,
                "low_conf_words": 0, "low_conf_pct": None,
            })
    return rows


#I would directly control the claude pages here, but the problem becomes empty or blank pages
#we should really try to filter those out with doc intel or algorythm's first then remove them, then
#record what pages should be sent to claude. otherwise page numbers will be off
def save_confidence(file_name, result):
    output = Path(csv_dir / f"{file_name}.csv")
    new_file = not os.path.exists(output)
    with open(output, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "file", "page", "word_count", "avg_confidence",
            "min_confidence", "low_conf_words", "low_conf_pct"])
        if new_file:
            w.writeheader()
        for row in get_page_confidence_scores(result):
            w.writerow({"file": file_name, **row})


def process_doc(doc):
    #here i will process the doucment based on doc intel itself. In fact, I want to work more of the pre processing in with this workflow. There will of course be processing from 

    #some ideas for balnk page detection
    #use an image processor and see how much of the page is white and not black or print colors
    #we won't run it on every page just ones that hit certain conditions. 

    #potentially look into rotating documents with doc intel 

    #probably mark these up on the csv as well with the page data, then have the pdf tools directly in here or see if we can import them from processing or some other folder

    #have it do so right away so that we don't have to adjust page numbers everytime we get rid of a page

    #this will basically just be a helper function that will either use imports or directly make changes within the document like rotation and removing blank pages

    print(doc)



def markForScan(doc):
    #Here I will process the actual csv file put the pages with low confidence in the db for the doc which will get it ready for scan.
    print(doc)



def doc_intel_scan(doc):
    with open("scan.pdf", "rb") as f:
        poller = client.begin_analyze_document("prebuilt-read", body=f)
        result = poller.result()
        save_confidence(result)  




 