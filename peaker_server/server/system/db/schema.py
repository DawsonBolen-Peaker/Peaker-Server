"""Tables in docs.db (NETWORK_ROOT/docs_db/docs.db). Created by database.init_db().

folders  one row per folder under one of the roots (system/paths.py ROOTS).
         relpath is relative to its root, named in root_folder, so the same
         relpath can be in two roots. has_new_files is 1 while any file at any
         depth under it is unscanned.
         sharepoint_name is the folder's name on SharePoint. It is the folder's
         own name, except for a top-level folder whose name another root's
         top-level folder or file already has there: every root is flattened
         onto the same SharePoint folder, so that one is renamed on SharePoint
         only, e.g. "Manuals (Product Info Library)". Set once, never changed.
SharePoint (folders and files): uploaded_to_sharepoint is 0 until the item is
         added to SharePoint, then 1, with sharepoint_id set to its SharePoint id.
batches  one row per run, named like its zip in batch_history. Files imported
         from the old tree.csv point at the batch named "unknown". A Batch API
         run's row is made when its results are applied (batch_api_<time>);
         message_batch_ids lists the Message Batches it took results from.
         What a run sent and what it cost (finance/usage.py):
           files_done / files_failed   files processed / failed in the run
           total_pages, image_pages    pages of the processed files, and how
                                       many of them went to Claude as images
           requests                    API requests (one per chunk)
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens
                                       as the API reported them, failed files included
           cost_usd                    from those tokens at the list prices in
                                       claudeAgent.PRICES (half for the Batch API)
files    one row per file. status: unscanned, in progress, scanned, failed,
         duplicate (same content as another file, never sent), or skipped
         (not a PDF: only PDFs go to Claude; uploaded to SharePoint as is).
           page_count    pages in the file when it was found (PDFs only).
           image_pages   pages sent to Claude as images when it was processed.
           input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
           cost_usd      what processing it took (the same as on batches).
           uuid          the file's permanent id. A rebuild writes it into the
                         PDF itself (/DocServerId), so the row follows the file
                         through renames and moves.
           sha256        content hash of the file as it is now (the rebuilt
                         PDF once scanned). Renaming or moving doesn't change it.
           api_batch_id  the Message Batches id while the file waits there.
           pages_removed blank pages the rebuild took out, e.g. "3,7".
           chunked       1 once the file's search chunks are in the chunks table.
           chunk_status  '' (never chunked), queued (waiting at the Batch API),
                         done, or failed (reason in chunk_error).
           doc_title, doc_summary, doc_keywords   document-level metadata for
                         search, from the scan (or the rebuilt PDF's own metadata).
meta     key/value: csv_import (when the tree.csv import ran), batch_jobs
         (Batch API scan jobs still waiting on results, as JSON), chunk_jobs
         (Batch API chunking jobs still waiting, as JSON).
chunks   search chunks of a scanned file (api/claude_chunk.py). Claude picks
         the boundaries (whole lines) and writes the title, summary and
         keywords; the text is cut from the file's own text by our code.
chunks_fts  FTS5 index over chunks, kept in step by triggers.
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS folders (
    folder_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    relpath       TEXT NOT NULL,
    name          TEXT NOT NULL,
    parent_id     INTEGER REFERENCES folders (folder_id) ON DELETE CASCADE,
    has_new_files INTEGER NOT NULL DEFAULT 0,
    uploaded_to_sharepoint INTEGER NOT NULL DEFAULT 0,
    sharepoint_id TEXT,
    root_folder   TEXT NOT NULL DEFAULT '',
    sharepoint_name TEXT,
    UNIQUE (root_folder, relpath)
);

CREATE TABLE IF NOT EXISTS batches (
    batch_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    status     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    file_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid          TEXT NOT NULL UNIQUE,
    relpath       TEXT NOT NULL,
    original_name TEXT NOT NULL,
    current_name  TEXT NOT NULL,
    folder_id     INTEGER REFERENCES folders (folder_id),
    status        TEXT NOT NULL,
    batch_id      INTEGER REFERENCES batches (batch_id),
    api_batch_id  TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT '',
    sha256        TEXT,
    pages_removed TEXT NOT NULL DEFAULT '',
    added_at      TEXT NOT NULL,
    scanned_at    TEXT,
    uploaded_to_sharepoint INTEGER NOT NULL DEFAULT 0,
    sharepoint_id TEXT,
    doc_type      TEXT NOT NULL DEFAULT '',
    departments   TEXT NOT NULL DEFAULT '',
    sharepoint_tagged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS files_relpath ON files (relpath);
CREATE INDEX IF NOT EXISTS files_status ON files (status);
CREATE INDEX IF NOT EXISTS files_sha256 ON files (sha256);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- The local OCR of each unscanned file, one row per page (processing/ocr.py).
-- Filled as soon as a file is found; a batch reads it to know which pages go
-- to Claude as images. Cleared when the file is processed.
--   score       OCR confidence 0-100 (100 for pages with their own digital text)
--   source      'text' (digital page) or 'ocr'
--   ink         share of dark pixels, 0-1; near 0 means a blank-looking page
--   send_image  1 when the page goes to Claude as an image (score under MIN_SCORE)
CREATE TABLE IF NOT EXISTS pages (
    file_id    INTEGER NOT NULL REFERENCES files (file_id) ON DELETE CASCADE,
    page       INTEGER NOT NULL,
    score      REAL NOT NULL,
    source     TEXT NOT NULL,
    ink        REAL NOT NULL DEFAULT 0,
    send_image INTEGER NOT NULL,
    text       TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (file_id, page)
);
"""

# Columns added after the first docs.db was made. CREATE TABLE IF NOT EXISTS
# leaves an existing table alone, so database.connect() adds any of these that
# are missing (ALTER TABLE ... ADD COLUMN). Existing rows get the default.
ADDED_COLUMNS = {
    "folders": {
        "uploaded_to_sharepoint": "INTEGER NOT NULL DEFAULT 0",
        "sharepoint_id": "TEXT",
        "root_folder": "TEXT NOT NULL DEFAULT ''",      # the root its relpath is under; '' = filled in by connect()
        "sharepoint_name": "TEXT",                      # its name on SharePoint; NULL = filled in by connect()
    },
    "files": {
        "uploaded_to_sharepoint": "INTEGER NOT NULL DEFAULT 0",
        "sharepoint_id": "TEXT",
        "doc_type": "TEXT NOT NULL DEFAULT ''",          # from the scan, for the SharePoint DocumentType column
        "departments": "TEXT NOT NULL DEFAULT ''",       # "; "-joined, for the SharePoint Department column
        "sharepoint_tagged": "INTEGER NOT NULL DEFAULT 0",
        "ocr_stamp": "TEXT NOT NULL DEFAULT ''",        # size:mtime of the file when its pages were read
        "ocr_status": "TEXT",                           # 'scanned' (has a text layer) or 'no ocr'; empty = not checked
        "ocr_service": "TEXT",                          # who did the OCR: 'claude' or 'scanner'; empty = none/unknown
        "system_version": "INTEGER",                    # version of this system that processed the file
        "root_folder": "TEXT",                          # the top folder the file came from, e.g. Locomotive_Scans
        "page_count": "INTEGER",                        # pages when found (PDFs); integrations/add_page_count.py fills old rows
        "image_pages": "INTEGER",                       # pages sent to Claude as images
        "input_tokens": "INTEGER",
        "output_tokens": "INTEGER",
        "cache_read_tokens": "INTEGER",
        "cache_write_tokens": "INTEGER",
        "cost_usd": "REAL",
        # search chunks (see CHUNK_SCHEMA)
        "chunked": "INTEGER NOT NULL DEFAULT 0",        # 1 once its chunks are saved
        "chunk_status": "TEXT NOT NULL DEFAULT ''",     # '', queued, done, failed
        "chunk_error": "TEXT NOT NULL DEFAULT ''",
        "chunk_batch_id": "TEXT NOT NULL DEFAULT ''",   # Message Batches id while queued
        "chunked_at": "TEXT",
        "chunk_count": "INTEGER",
        "chunk_cost_usd": "REAL",                       # what the last chunking took (standalone runs)
        "doc_title": "TEXT NOT NULL DEFAULT ''",
        "doc_summary": "TEXT NOT NULL DEFAULT ''",
        "doc_keywords": "TEXT NOT NULL DEFAULT ''",     # "; "-joined
    },
    "batches": {
        "api": "TEXT",                                  # 'batch' (Message Batches) or 'regular'
        "model": "TEXT",
        "message_batch_ids": "TEXT NOT NULL DEFAULT ''",
        "files_done": "INTEGER NOT NULL DEFAULT 0",
        "files_failed": "INTEGER NOT NULL DEFAULT 0",
        "total_pages": "INTEGER NOT NULL DEFAULT 0",
        "image_pages": "INTEGER NOT NULL DEFAULT 0",
        "requests": "INTEGER NOT NULL DEFAULT 0",
        "input_tokens": "INTEGER NOT NULL DEFAULT 0",
        "output_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cache_read_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cache_write_tokens": "INTEGER NOT NULL DEFAULT 0",
        "cost_usd": "REAL NOT NULL DEFAULT 0",
        "finished_at": "TEXT",
    },
}

INDEXES_ON_ADDED = """
CREATE INDEX IF NOT EXISTS files_sharepoint ON files (uploaded_to_sharepoint);
CREATE INDEX IF NOT EXISTS files_chunked ON files (chunked);
"""

# Search chunks. Run after the files columns are in place (database.connect).
#   chunk_uuid     the chunk's permanent id (for links, embeddings, ...)
#   chunk_index    order in the document, from 0
#   page_start/end pages of the PDF as it is now (blank pages already removed)
#   start_line/end_line  the numbered lines Claude chose, in its request
#   text           the chunk's text, cut from the document by our code
#   token_count    rough (characters / 4)
#   keywords       JSON list
#   chunk_type     text, table, drawing, procedure, parts_list, ...
#   source         'scan' (from the main record_scan request) or 'chunker'
#                  (the standalone chunking job, from the rebuilt PDF's text)
#   doc_title, doc_keywords  copied from the file so one FTS query also
#                  matches document-level terms
#   embedding      left empty for now (a local model + sqlite-vec later)
CHUNK_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id             INTEGER PRIMARY KEY,
    chunk_uuid     TEXT NOT NULL UNIQUE,
    file_id        INTEGER NOT NULL REFERENCES files (file_id) ON DELETE CASCADE,
    chunk_index    INTEGER NOT NULL,
    page_start     INTEGER,
    page_end       INTEGER,
    start_line     INTEGER,
    end_line       INTEGER,
    text           TEXT NOT NULL,
    token_count    INTEGER,
    section_title  TEXT NOT NULL DEFAULT '',
    summary        TEXT NOT NULL DEFAULT '',
    keywords       TEXT NOT NULL DEFAULT '[]',
    chunk_type     TEXT NOT NULL DEFAULT '',
    doc_title      TEXT NOT NULL DEFAULT '',
    doc_keywords   TEXT NOT NULL DEFAULT '',
    source         TEXT NOT NULL DEFAULT '',
    model          TEXT NOT NULL DEFAULT '',
    chunker_version INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL,
    embedding      BLOB,
    UNIQUE (file_id, chunk_index)
);
CREATE INDEX IF NOT EXISTS chunks_file ON chunks (file_id);
CREATE INDEX IF NOT EXISTS chunks_type ON chunks (chunk_type);

CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text, section_title, summary, keywords, doc_title, doc_keywords,
    content='chunks', content_rowid='id', tokenize='porter unicode61');

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts (rowid, text, section_title, summary, keywords, doc_title, doc_keywords)
    VALUES (new.id, new.text, new.section_title, new.summary, new.keywords, new.doc_title, new.doc_keywords);
END;
CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, section_title, summary, keywords, doc_title, doc_keywords)
    VALUES ('delete', old.id, old.text, old.section_title, old.summary, old.keywords, old.doc_title, old.doc_keywords);
END;
CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts (chunks_fts, rowid, text, section_title, summary, keywords, doc_title, doc_keywords)
    VALUES ('delete', old.id, old.text, old.section_title, old.summary, old.keywords, old.doc_title, old.doc_keywords);
    INSERT INTO chunks_fts (rowid, text, section_title, summary, keywords, doc_title, doc_keywords)
    VALUES (new.id, new.text, new.section_title, new.summary, new.keywords, new.doc_title, new.doc_keywords);
END;
"""
