#specifically for generating csv data from db, not to be used in app necassarily just
#for example getting a list of all scanned documents
import pandas as pd
import csv
import sys
import pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))     # server/
from system.db.dbfunctions.queries.queries import get_scanned_files



df = get_scanned_files()


df.to_csv("output.csv", index=False)


