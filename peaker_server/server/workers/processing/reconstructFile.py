
import json, os, re, shutil, sys, textwrap
from pathlib import Path
from pypdf import PdfReader, PdfWriter, PageObject
from pypdf.generic import DictionaryObject, NameObject, StreamObject

PDF_ID_KEY = "/DocServerId"
LEGACY_ID_KEY = "/FileScannerId"     
FONT = "/DSInvisible"
FONT_SIZE = 6
CHUNK_RE = re.compile(r"^--- Original pages (\d+)-(\d+) ---$", re.M)
PAGE_RE = re.compile(r"^\[\[Page (\d+)\]\]$", re.M)


def read_pdf_id(path):
    try:
        meta = PdfReader(str(path)).metadata or {}
    except Exception:
        return None
    value = meta.get(PDF_ID_KEY) or meta.get(LEGACY_ID_KEY)
    return str(value) if value else None

#metadata

def keyword_list(data: dict) -> list[str]:
    """Every short searchable term in the scan result, without duplicates."""
    words = []
    for key in ("doc_type", "revision"):
        words.append(data.get(key) or "")
    for key in ("keywords", "keywords_inferred", "categories", "new_categories", "departments",
                "manufacturers", "models", "components", "customers", "vendors",
                "locations", "projects", "standards", "folder_keywords"):
        words += [w for w in data.get(key) or [] if isinstance(w, str)]
    words += [i.get("value", "") for i in data.get("identifiers") or [] if isinstance(i, dict)]
    seen, out = set(), []
    for w in (w.strip() for w in words):
        if w and w.casefold() not in seen:
            seen.add(w.casefold())
            out.append(w)
    return out


def build_metadata(data: dict, file_id: str) -> dict:
    return {
        "/Title": data.get("full_title") or data.get("title") or "",
        "/Subject": data.get("summary") or "",
        "/Keywords": "; ".join(keyword_list(data)),
        PDF_ID_KEY: file_id,
    }


# ---------- invisible text layer ----------
def text_by_page(full_text: str, page_count: int) -> dict[int, str]:
    """{page_index: text}. Uses [[Page N]] markers (inside optional
    '--- Original pages a-b ---' chunk headers) when the scan has them;
    otherwise each chunk's text goes on its first page, or all on page 1."""
    pages: dict[int, list[str]] = {}

    def put(page_no, text):
        if text.strip():
            i = min(max(page_no, 1), page_count) - 1
            pages.setdefault(i, []).append(text.strip())

    chunks = list(CHUNK_RE.finditer(full_text))
    if chunks:
        parts = [(int(m.group(1)), full_text[m.end():chunks[k + 1].start() if k + 1 < len(chunks) else None])
                 for k, m in enumerate(chunks)]
    else:
        parts = [(1, full_text)]

    for first_page, text in parts:
        marks = list(PAGE_RE.finditer(text))
        if not marks:
            put(first_page, text)
            continue
        put(first_page, text[:marks[0].start()])
        for k, m in enumerate(marks):
            end = marks[k + 1].start() if k + 1 < len(marks) else None
            put(first_page + int(m.group(1)) - 1, text[m.end():end])
    return {i: "\n".join(t) for i, t in pages.items()}


def pdf_string(line: str) -> str:
    """A PDF literal string in WinAnsi encoding (characters it lacks become '?')."""
    out = []
    for b in line.encode("cp1252", errors="replace"):
        c = chr(b)
        if c in "\\()":
            out.append("\\" + c)
        elif 32 <= b < 127:
            out.append(c)
        else:
            out.append(f"\\{b:03o}")
    return "(" + "".join(out) + ")"


def invisible_overlay(writer: PdfWriter, page: PageObject, text: str) -> PageObject:
    """A blank page the size of `page` holding `text` in render mode 3 (invisible)."""
    box = page.mediabox
    left, top = float(box.left) + 2, float(box.top) - FONT_SIZE - 2
    width = max(int((float(box.width) - 4) / (FONT_SIZE * 0.5)), 20)   
    lines = [w for l in text.splitlines() if l.strip() for w in textwrap.wrap(l, width)]
    leading = min(FONT_SIZE, (float(box.height) - FONT_SIZE - 4) / max(len(lines), 1))
    ops = ["q", "BT", "3 Tr", f"{FONT} {FONT_SIZE} Tf", f"{leading:.3f} TL", f"1 0 0 1 {left:.2f} {top:.2f} Tm"]
    for line in lines:
        ops.append(f"{pdf_string(line)} Tj T*")
    ops += ["ET", "Q"]

    overlay = PageObject.create_blank_page(width=box.width, height=box.height)
    overlay.mediabox = box
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
        NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
    })
    overlay[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject(FONT): writer._add_object(font)})})
    stream = StreamObject()
    stream.set_data("\n".join(ops).encode("latin-1"))
    overlay[NameObject("/Contents")] = writer._add_object(stream)
    return overlay


# ---------- rotate and crop ----------

def page_rotations(rotations, page_count: int) -> dict[int, int]:
    """{page_no: clockwise degrees} from Claude's rotations, valid entries only."""
    out = {}
    for r in rotations or []:
        if isinstance(r, dict) and isinstance(r.get("page"), int) and 1 <= r["page"] <= page_count \
                and r.get("degrees") in (90, 180, 270):
            out[r["page"]] = r["degrees"]
    return out


def cleaned_page(src: Path, page_no: int, degrees: int) -> PageObject:
    import io
    import pymupdf
    from image.image_corrector import clean_page, DPI  

    with pymupdf.open(str(src)) as doc:
        pix = doc[page_no - 1].get_pixmap(dpi=DPI)
        from PIL import Image
        img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    if degrees:
        img = img.rotate(-degrees, expand=True)            
    img = clean_page(img).convert("RGB")
    jpeg = io.BytesIO()
    img.save(jpeg, "JPEG", quality=85)

    width, height = img.width * 72 / DPI, img.height * 72 / DPI
    with pymupdf.open() as out:
        page = out.new_page(width=width, height=height)
        page.insert_image(page.rect, stream=jpeg.getvalue())
        data = out.tobytes()
    return PdfReader(io.BytesIO(data)).pages[0]


def fix_pages(writer: PdfWriter, src: Path, data: dict) -> tuple[list[int], list[int], list[str]]:
    """Apply Claude's rotations and crop_pages to `writer` in place.
    Returns (rotated pages, cropped pages, notes about pages that could not be fixed)."""
    page_count = len(writer.pages)
    rotations = page_rotations(data.get("rotations"), page_count)
    crops = sorted({p for p in data.get("crop_pages") or [] if isinstance(p, int) and 1 <= p <= page_count})
    rotated, cropped, notes = [], [], []
    for page_no in crops:
        try:
            new_page = cleaned_page(src, page_no, rotations.get(page_no, 0))
            writer.insert_page(new_page, index=page_no - 1)
            del writer.pages[page_no]
            cropped.append(page_no)
            if page_no in rotations:
                rotated.append(page_no)
        except Exception as e:
            notes.append(f"page {page_no} not cropped: {e}")
            print(f"  could not crop page {page_no}: {e}")
    for page_no, degrees in rotations.items():
        if page_no not in cropped:
            writer.pages[page_no - 1].rotate(degrees)
            rotated.append(page_no)
    return sorted(rotated), cropped, notes


def pages_to_remove(blank_pages, page_count: int) -> list[int]:
    """Valid 1-based page numbers from Claude's blank_pages. Never all of them."""
    pages = sorted({p for p in blank_pages or [] if isinstance(p, int) and 1 <= p <= page_count})
    if len(pages) >= page_count:
        print(f"  Claude marked every page blank; keeping all {page_count} pages")
        return []
    return pages


# ---------- one file ----------

def reconstruct_pdf(src, dest, data: dict, file_id: str) -> dict:
    """Write the rebuilt PDF to `dest` (which may be `src`). The new file is
    written next to dest first and only then put in place, so a failure leaves
    src untouched. Returns {"removed", "rotated", "cropped", "notes"}, all page
    numbers of the original scan."""
    src, dest = Path(src), Path(dest)
    reader = PdfReader(str(src))
    if reader.is_encrypted:
        raise ValueError("PDF is encrypted")

    writer = PdfWriter(clone_from=reader)
    rotated, cropped, notes = fix_pages(writer, src, data)
    page_count = len(writer.pages)
    for i, text in text_by_page(data.get("full_text") or "", page_count).items():
        page = writer.pages[i]
        page.merge_page(invisible_overlay(writer, page, text))
    removed = pages_to_remove(data.get("blank_pages"), page_count)
    for page_no in reversed(removed):
        del writer.pages[page_no - 1]
    writer.add_metadata({**{k: v for k, v in (reader.metadata or {}).items()},
                         **build_metadata(data, file_id)})

    tmp = dest.with_name(dest.name + ".tmp")
    try:
        with tmp.open("wb") as f:
            writer.write(f)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return {"removed": removed, "rotated": rotated, "cropped": cropped, "notes": notes}


if __name__ == "__main__":
    if len(sys.argv) != 4:
        sys.exit(__doc__)
    sidecar, src, pdf = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    shutil.copy2(src, pdf)
    result = reconstruct_pdf(pdf, pdf, data, data.get("id") or sidecar.stem)
    print("written ->", pdf, result)
