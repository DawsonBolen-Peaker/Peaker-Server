import os
import sys
from pathlib import Path
from dotenv import load_dotenv

if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
    ENV_PATH = APP_DIR / ".env"
    SERVER_DIR = APP_DIR
else:
    # Development: .env inside server/
    SERVER_DIR = Path(__file__).resolve().parent.parent
    ENV_PATH = SERVER_DIR / ".env"

load_dotenv(ENV_PATH, override=True)

NETWORK_IP = os.environ.get("NETWORK_IP")


def _network_root() -> Path:
    ip = os.environ.get("NETWORK_IP", "").strip()
    if not ip:
        raise RuntimeError(
            f"NETWORK_IP is not set. Check that the environment file exists: {ENV_PATH}"
        )
    return Path(rf"\\{ip}\PSIMain\PSI_Files")


NETWORK_ROOT = _network_root()

def _dir(env_name: str, default_name: str) -> Path:
    value = os.environ.get(env_name, "").strip()
    return Path(value).expanduser() if value else NETWORK_ROOT / default_name

DOCUMENTS_DIR = _dir("DOCUMENTS_DIR", "Locomotive_Scans")
DB_DIR = _dir("DOCS_DB_DIR", "docs_db")
OUTPUT_DIR = _dir("OUTPUT_DIR", "output")
HISTORY_DIR = _dir("HISTORY_DIR", "batch_history")
PRODUCT_LIB_DIR = Path(rf"\\{NETWORK_IP}\PSIMain\Distribution\Product Info Library")

DB_PATH = DB_DIR / "docs.db"
CONFIG_PATH = DB_DIR / "config.json"
DEFAULT_CONFIG_PATH = SERVER_DIR.parent / "config" / "default.json"
LEGACY_CSV_PATH = SERVER_DIR.parent / "config" / "tree.csv"

# Top folders files come from. Every one of them is flattened onto the same
# SharePoint folder (the roots themselves aren't on SharePoint), and a file's or
# folder's relpath is relative to its own root, which docs.db keeps in
# root_folder (the root's folder name). Root names must be different.
ROOTS = [DOCUMENTS_DIR, PRODUCT_LIB_DIR]
ROOT_DIRS = {root.name: root for root in ROOTS}
if len(ROOT_DIRS) != len(ROOTS):
    raise RuntimeError(f"Two roots share a folder name: {[str(r) for r in ROOTS]}")


def root_dir(root_folder) -> Path:
    """The root a root_folder name stands for. Rows from before there were
    several roots have no root_folder; they are in the documents folder."""
    if not isinstance(root_folder, str) or not root_folder:
        return DOCUMENTS_DIR
    try:
        return ROOT_DIRS[root_folder]
    except KeyError:
        raise RuntimeError(f"Unknown root folder {root_folder!r}; known roots: {', '.join(ROOT_DIRS)}") from None


def file_path(row) -> Path:
    """Where a files row (dict, Series or sqlite3.Row with relpath and
    root_folder) is on disk."""
    keys = row.keys() if hasattr(row, "keys") else ()
    return root_dir(row["root_folder"] if "root_folder" in keys else None) / row["relpath"]
