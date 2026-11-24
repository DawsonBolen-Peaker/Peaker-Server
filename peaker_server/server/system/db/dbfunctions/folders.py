"""Rows in the folders table, and the names already used at the top of the SharePoint folder."""

from pathlib import Path

from system.db.dbfunctions.database import UNSCANNED, DUPLICATE, MAIN_ROOT, connect
from system.db.dbfunctions.helpers import is_top_level, free_name


def sharepoint_top_level_names(conn, not_folder_id=None, not_file_id=None) -> set:
    """Names (casefolded; SharePoint ignores case) already used directly in the
    SharePoint folder every root is flattened onto: top-level folders by their
    SharePoint name and top-level files that can go up (not duplicates)."""
    names = {r["n"].casefold() for r in conn.execute(
        "SELECT COALESCE(sharepoint_name, name) AS n FROM folders WHERE parent_id IS NULL AND folder_id IS NOT ?",
        (not_folder_id,))}
    for r in conn.execute("SELECT relpath, COALESCE(sharepoint_name, current_name) AS n FROM files"
                          " WHERE status != ? AND file_id IS NOT ?", (DUPLICATE, not_file_id)):
        if is_top_level(r["relpath"]):
            names.add(r["n"].casefold())
    return names


def folder_id_for(conn, relpath: str, root_folder: str = MAIN_ROOT):
    """folder_id of the folder holding relpath in that root (None at the top of the root)."""
    if is_top_level(relpath):
        return None
    row = conn.execute("SELECT folder_id FROM folders WHERE root_folder = ? AND relpath = ?",
                       (root_folder or MAIN_ROOT, Path(relpath).parent.as_posix())).fetchone()
    return row["folder_id"] if row else None




def add_folder(conn, relpath: str, root_folder: str = MAIN_ROOT) -> None:
    """Add a folder. A top-level folder whose name another top-level folder or
    file already has on SharePoint gets a different SharePoint name; the folder
    itself (and every relpath under it) keeps its real name."""
    name = Path(relpath).name
    sharepoint_name = name
    if is_top_level(relpath):
        sharepoint_name = free_name(name, root_folder, sharepoint_top_level_names(conn))
        if sharepoint_name != name:
            print(f"Folder {root_folder}/{relpath}: another root already has a top-level {name!r},"
                  f" so on SharePoint it is called {sharepoint_name!r}")
    conn.execute("INSERT OR IGNORE INTO folders (relpath, name, parent_id, root_folder, sharepoint_name)"
                 " VALUES (?, ?, ?, ?, ?)",
                 (relpath, name, folder_id_for(conn, relpath, root_folder), root_folder, sharepoint_name))




def update_folder_flags(conn) -> None:
    """has_new_files = 1 if any file at any depth under the folder is unscanned."""
    unscanned = [(r["root_folder"] or MAIN_ROOT, r["relpath"])
                 for r in conn.execute("SELECT root_folder, relpath FROM files WHERE status = ?", (UNSCANNED,))]
    for f in conn.execute("SELECT folder_id, root_folder, relpath FROM folders").fetchall():
        prefix = f["relpath"] + "/"
        flag = int(any(root == f["root_folder"] and p.startswith(prefix) for root, p in unscanned))
        conn.execute("UPDATE folders SET has_new_files = ? WHERE folder_id = ?", (flag, f["folder_id"]))


def mark_folder_sharepoint(folder_id: int, sp_id: str, sharepoint_name: str | None = None) -> None:
    """Record that a folder (by folder_id) is on SharePoint with id sp_id, and
    the name it got there when given (SharePoint can add a number of its own)."""
    with connect() as conn:
        cur = conn.execute("UPDATE folders SET uploaded_to_sharepoint = 1, sharepoint_id = ?,"
                           " sharepoint_name = COALESCE(?, sharepoint_name) WHERE folder_id = ?",
                           (sp_id, sharepoint_name, folder_id))
        if cur.rowcount == 0:
            raise ValueError(f"No folder in docs.db with id: {folder_id}")


def rename_folder_on_sharepoint(folder_id: int, also_taken: str = "") -> str:
    """Give a top-level folder a new SharePoint name (the folder on disk keeps
    its name) because its current one is taken there. Returns the new name."""
    with connect() as conn:
        f = conn.execute("SELECT * FROM folders WHERE folder_id = ?", (folder_id,)).fetchone()
        taken = sharepoint_top_level_names(conn, not_folder_id=folder_id)
        taken |= {(f["sharepoint_name"] or f["name"]).casefold(), also_taken.casefold()}
        name = free_name(f["name"], f["root_folder"], taken)
        conn.execute("UPDATE folders SET sharepoint_name = ? WHERE folder_id = ?", (name, folder_id))
    return name


def top_level_names_for_folder(folder_id: int) -> set:
    """sharepoint_top_level_names() leaving out this folder."""
    with connect() as conn:
        return sharepoint_top_level_names(conn, not_folder_id=folder_id)

#this was already ran so no need to drop chunks again
# def migrate_chunks(conn) -> None:
#     """The first chunks table was only a sketch and never held anything; swap
#     it (and its FTS index) for the current one."""
#     have = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)")}
#     if have and "chunk_uuid" not in have:
#         conn.executescript("DROP TABLE IF EXISTS chunks_fts; DROP TABLE IF EXISTS chunks;")
#         print("docs.db: replaced the old chunks table with the search chunks table")

