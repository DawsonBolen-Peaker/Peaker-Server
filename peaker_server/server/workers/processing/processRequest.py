import json, re, shutil
from pathlib import Path
from system.paths import DOCUMENTS_DIR, root_dir
from system.db.dbfunctions.helpers import sha256_of, is_scanner_default_name
from system.db.dbfunctions.files import top_level_names_for
from workers.processing.reconstructFile import reconstruct_pdf
from workers.processing import ocr

def safe_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r\t]', "", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(". ")
    return name[:150] or "untitled"

def folder_metadata(relpath: str) -> dict:
    """Where the file now lives, so a search for any of its folder names finds it."""
    parts = Path(relpath).parent.parts
    return {
        "folder_path": "/".join(parts) or "root",
        "folder": parts[-1] if parts else "root",
        "parent_folder": parts[-2] if len(parts) > 1 else "root",
        "folder_keywords": list(parts),
    }

def can_rename(src: Path, root: Path) -> bool:
    """Claude's suggested_filename is used only for files in Locomotive_Scans
    (DOCUMENTS_DIR) that still have a scanner's default name (Doc0337,
    Onesided0085, upstairs_double_sided_0001, ...). Every other name is kept."""
    return root == DOCUMENTS_DIR and is_scanner_default_name(src.name)


def can_move(src: Path, root: Path) -> bool:
    """Only files at the top of Locomotive_Scans are filed into a folder. Files
    in the other roots are never moved or renamed."""
    return root == DOCUMENTS_DIR and src.parent == DOCUMENTS_DIR


def process_document(file_id, relpath, data, folders, archive_dir, out_dir, root_folder=None) -> dict:
    """Archive the original, rebuild it under its new name and folder, and write
    the sidecar. relpath is relative to its root (root_folder; the documents
    folder when not given), and the returned relpath is in the same root.
    Returns {"relpath", "sha256", "pages_removed", "note", "doc_type",
    "departments"} for database.mark_processed."""
    root = root_dir(root_folder)
    src = root / relpath
    if not src.is_file():
        raise FileNotFoundError(relpath)

    pages = ocr.load_pages(file_id)
    if pages and not data.get("full_text"):
        corrections = {t["page"]: t["text"] for t in data.get("page_text") or []}
        data = {**data, "full_text": ocr.full_text(pages, corrections),
                "ocr_scores": {p["page"]: p["score"] for p in pages}}

    # 1. keep the untouched original in the batch area, tagged with its id
    shutil.copy2(src, archive_dir / f"{file_id}__{src.name}")

    # 2. only move files that sit at the top of the documents folder, and only
    #    to one of its folders that exists (folders are the documents folder's
    #    only; files in the other roots stay in their folder)
    folder = data.get("destination_folder", "NONE")
    dest_dir = DOCUMENTS_DIR / folder if (can_move(src, root) and folder in folders) else src.parent

    # 3. name: Claude's suggestion only replaces a scanner default name (see
    #    can_rename); otherwise the file keeps its own. (1), (2) is the
    #    last-resort tiebreaker. A Locomotive_Scans file left at its top also
    #    needs a name no other root's top-level file or folder has, since all
    #    roots share one folder on SharePoint. Files in the other roots keep
    #    their name and place no matter what; a SharePoint name clash for them
    #    is handled at upload with files.sharepoint_name.
    if root != DOCUMENTS_DIR:
        dest = src
    else:
        base = safe_filename(data.get("suggested_filename") or src.stem) if can_rename(src, root) else src.stem
        taken = top_level_names_for(file_id) if dest_dir == root else set()
        dest, i = dest_dir / f"{base}{src.suffix}", 1
        while (dest.exists() and dest != src) or (dest != src and dest.name.casefold() in taken):
            dest = dest_dir / f"{base} ({i}){src.suffix}"
            i += 1
    new_relpath = dest.relative_to(root).as_posix()

    # 4. rebuild the PDF straight away at its new name: blank pages out, metadata,
    #    uuid and corrected text in, and flagged pages turned and cropped first. If that fails the scan result is still kept
    #    (sidecar + docs.db) and the file is just moved as it was.
    removed, rotated, cropped, note = [], [], [], ""
    if src.suffix.lower() == ".pdf":
        try:
            rebuilt = reconstruct_pdf(src, dest, {**data, **folder_metadata(new_relpath)}, file_id)
            removed, rotated, cropped = rebuilt["removed"], rebuilt["rotated"], rebuilt["cropped"]
            note = "; ".join(rebuilt["notes"])
            if dest != src:
                src.unlink()
        except Exception as e:
            note = f"PDF not rebuilt: {e}"
            print(f"  could not rebuild {src.name}: {e}")
    if dest != src and src.exists():
        shutil.move(str(src), str(dest))
    if removed:
        print(f"  removed blank pages {removed} from {dest.name}")
    if rotated or cropped:
        print(f"  turned pages {rotated}, cropped pages {cropped} in {dest.name}")
    digest = sha256_of(dest)

    # 5. records go in the batch's output folder, NOT in documents/
    (out_dir / "sidecars").mkdir(parents=True, exist_ok=True)
    location = folder_metadata(new_relpath)
    original = folder_metadata(relpath)
    location["original_folder_path"] = original["folder_path"]
    location["folder_keywords"] = list(dict.fromkeys(location["folder_keywords"] + original["folder_keywords"]))
    record = {"id": file_id, "original_relpath": relpath, "new_relpath": new_relpath,
              **location, **data, "pages_removed": removed,
              "pages_rotated": rotated, "pages_cropped": cropped, "pdf_rebuilt": not note.startswith("PDF not rebuilt"), "sha256": digest}
    (out_dir / "sidecars" / f"{file_id}.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    with open(out_dir / "manifest.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({"id": file_id, "from": relpath, "to": new_relpath, "sha256": digest}) + "\n")
    return {"relpath": new_relpath, "sha256": digest, "pages_removed": removed, "note": note,
            "doc_type": data.get("doc_type") or "", "departments": data.get("departments") or []}
