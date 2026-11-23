#for editing db
from contextlib import contextmanager
import sqlite3
from system.paths import DB_PATH

#for now everything in the db that has been added to sharepoint should be marked as processed some are left as failed even though they should not be

conn = sqlite3.connect(DB_PATH)

def updateFailed():
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE files SET status = ? WHERE status = ?",
        ("scanned", "failed")
    )

    conn.commit()
    conn.close()

#need to research this more that is why i print
def updateNoOcrCols():
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE files SET ocr_status = ? AND ocr_service = ? WHERE ocr_status = ? AND "
    )






def printFiles():
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM files")
    
    # 1. Fetch all matching rows from the query
    rows = cursor.fetchall()
    
    # 2. Loop through and print each row
    for row in rows:
        print(row)



if __name__ == "__main__":
    printFiles()