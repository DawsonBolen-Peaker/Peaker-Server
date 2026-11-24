import hashlib, re
from datetime import datetime
from pathlib import Path

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")

def rel(p: Path, r: Path) -> str:
    return p.relative_to(r).as_posix()

def sha256_of(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def yes_no(value):
    """'yes' / 'no' / True / False / 1 / 0 -> 1 or 0; anything else (None, 'any', '') -> None."""
    if value in (True, 1, "1", "yes", "true"):
        return 1
    if value in (False, 0, "0", "no", "false"):
        return 0
    return None

def join_departments(departments) -> str:
    return "; ".join(d for d in departments or [] if isinstance(d, str) and d.strip())

def is_pdf(name: str) -> bool:
    return Path(name).suffix.lower() == ".pdf"

SYSTEM_FILES = {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store"}


def is_system_file(name: str) -> bool:
    name = name.lower()
    return name in SYSTEM_FILES or name.startswith(("~$", "._"))

accepted_file_types = [
    ".pdf",
    ".docx", ".doc",
    ".xlsx", ".xls", ".csv",
    ".pptx", ".ppt",
    ".jpg", ".jpeg", ".png", ".tif", ".tiff",
    ".txt"
]

def is_accepted_file(n) -> bool:
    """File types picked up at all (any case: Scan.PDF counts)."""
    return Path(n).suffix.lower() in accepted_file_types


# ---------- scanner default names ----------
# The only names Claude may replace: what the scanners call a file, e.g. Doc0337,
# Onesided0085, upstairs_double_sided_0001, upstairs_one_sided_0001, and copies
# like upstairs_double_sided_0002-1 or "Doc0337 (2)". Every other name is a
# person's and stays as it is.
SCANNER_NAME = re.compile(
    r"(?:upstairs[ _-]?)?(?:doc|one[ _-]?sided|double[ _-]?sided)[ _-]?\d+(?:[ _-]\d+)?(?:\s*\(\d+\))?",
    re.IGNORECASE)


def is_scanner_default_name(name: str) -> bool:
    """True when a file name (with or without its extension) is a scanner's
    numbered default name, so Claude may give it a real one."""
    stem = Path(name).stem if Path(name).suffix.lower() in accepted_file_types else name
    return bool(SCANNER_NAME.fullmatch(stem.strip()))

# ---------- names on SharePoint ----------

def is_top_level(relpath: str) -> bool:
    """Directly inside a root. Every root is flattened onto the same SharePoint
    folder, so only these can have the same name on SharePoint; deeper ones sit
    under a parent folder that is already unique there."""
    return Path(relpath).parent.as_posix() in ("", ".")

def free_name(name: str, root_folder: str, taken, is_file: bool = False) -> str:
    """name if it isn't taken, else name with its root added, e.g.
    "Manuals (Product Info Library)" or "Price list (Product Info Library).pdf",
    with a number after the root if that is taken too."""
    if name.casefold() not in taken:
        return name
    stem, suffix = (Path(name).stem, Path(name).suffix) if is_file else (name, "")
    n = 1
    while True:
        tag = root_folder if n == 1 else f"{root_folder} {n}"
        candidate = f"{stem} ({tag}){suffix}"
        if candidate.casefold() not in taken:
            return candidate
        n += 1
