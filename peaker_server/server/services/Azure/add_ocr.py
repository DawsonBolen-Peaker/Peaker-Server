import io
import logging
import os
from pathlib import Path
import numpy as np
import pymupdf
from azure.core.credentials import AzureKeyCredential
from azure.ai.documentintelligence import DocumentIntelligenceClient
from PIL import Image

from system.db.dbfunctions.files import getDocsForIntel, update_ocr_service_intel, save_pages
from system.paths import file_path

log = logging.getLogger(__name__)

INK_DPI = 40
LOW_CONFIDENCE = 75          
BLANK_INK_THRESHOLD = 0.005

client = DocumentIntelligenceClient(
    endpoint=os.environ["DOCUMENTINTELLIGENCE_ENDPOINT"],
    credential=AzureKeyCredential(os.environ["DOCUMENTINTELLIGENCE_API_KEY"]),
)


def get_ink_pages(path):
    page_inks = []
    with pymupdf.open(path) as file:
        for page in file:
            pix = page.get_pixmap(dpi=INK_DPI, colorspace=pymupdf.csGRAY, alpha=False)
            img = np.frombuffer(pix.samples, dtype=np.uint8)
            page_inks.append(round(float(np.mean(img < 128)), 4) if img.size else 0.0)
    return page_inks


def get_page_confidence_scores(path, result):
    rows = []
    ink_vals = get_ink_pages(path)
    azure_pages = {p.page_number: p for p in (result.pages or [])}

    for page_number, ink in enumerate(ink_vals, start=1):
        page = azure_pages.get(page_number)
        scores = [w.confidence for w in (page.words or [])] if page else []

        if scores:
            avg = sum(scores) / len(scores) * 100
            rows.append({
                "page": page_number,
                "score": avg,
                "source": "doc_intel",
                "ink": ink,
                "send_image": avg <= LOW_CONFIDENCE,
                "text": "\n".join(l.content for l in (page.lines or [])),
                "angle": page.angle or 0,
                "words": len(scores),          # fix_orientation only trusts the angle with enough words
            })
        else:
            rows.append({
                "page": page_number,
                "score": 0.0,
                "source": "doc_intel",
                "ink": ink,
                "send_image": ink > BLANK_INK_THRESHOLD,
                "text": "",
                "angle": (page.angle or 0) if page else 0,
                "words": 0,
            })
    return rows


def doc_intel_scan(doc):
    # relpath is relative to the file's own root (Locomotive_Scans, Product Info Library, ...)
    path = file_path(doc)
    with open(path, "rb") as f:
        poller = client.begin_analyze_document("prebuilt-read", body=f)
        result = poller.result()
    rows = get_page_confidence_scores(path, result)
    save_pages(doc["uuid"], "doc_intel", [{k: v for k, v in r.items() if k not in ("angle", "words")} for r in rows])
    return result


#i'd like to take out potentially blank pages here 

#lets set this every time so we don't have to contantly use pymupdf to read everypage


#here pass in a dict array of page#, angle, and 
MAX_LEFTOVER = 15    # farther than this from a multiple of 90 = flag for review
TILT_CUTOFF = 10     # ignore leftover tilt smaller than this
MIN_WORDS = 20       # don't trust the angle on pages with fewer words


def split_angle(angle):
    """Split Azure's angle into (quarter_turns, leftover_tilt)."""
    quarter = round(angle / 90)
    return quarter, angle - quarter * 90


def straighten_image(file, page, tilt):
    imgs = page.get_images(full=True)
    if len(imgs) != 1:
        return False                          
    xref = imgs[0][0]

    pix = pymupdf.Pixmap(file, xref)
    if pix.alpha:
        pix = pymupdf.Pixmap(pix, 0)
    if pix.colorspace.n not in (1, 3):
        pix = pymupdf.Pixmap(pymupdf.csRGB, pix)

    mode = "L" if pix.n == 1 else "RGB"
    img = Image.frombytes(mode, (pix.width, pix.height), pix.samples)
    white = 255 if mode == "L" else (255, 255, 255)
    img = img.rotate(tilt, resample=Image.BICUBIC, fillcolor=white)  # no -1 here

    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=90)
    page.replace_image(xref, stream=buf.getvalue())
    return True


def fix_orientation(path, pages_info):
    """Turns sideways pages and straightens tilted ones. Returns True if the file changed."""
    tmp = path.with_suffix(".rot.pdf")
    changed = False
    try:
        with pymupdf.open(path) as file:
            for i, page in enumerate(file):
                info = pages_info[i]
                if info["words"] < MIN_WORDS:
                    continue                  # blank or nearly blank, angle isn't reliable

                quarter, leftover = split_angle(info["angle"])
                if abs(leftover) > MAX_LEFTOVER:
                    log.warning("page %s angle %s needs review", i + 1, info["angle"])
                    continue

                if quarter % 4:               
                    page.set_rotation((page.rotation - quarter * 90) % 360)
                    changed = True

                if abs(leftover) >= TILT_CUTOFF:
                    if straighten_image(file, page, leftover):
                        changed = True

            if changed:
                file.save(tmp, garbage=3, deflate=True)

        if changed:
            tmp.replace(path)                 
    finally:
        tmp.unlink(missing_ok=True)
    return changed




def analyze(path):
    with open(path, "rb") as f:
        return client.begin_analyze_document("prebuilt-read", body=f).result()
     

def write_text_layer(path, result):
    """Strip the old OCR text and add Azure's text, then replace the file."""
    azure_pages = {p.page_number - 1: p for p in (result.pages or [])}
    tmp_path = path.with_suffix(".tmp.pdf")

    try:
        with pymupdf.open(path) as scan:
            for page_num, pdf_page in enumerate(scan):
                for block in pdf_page.get_text("blocks"):
                    if block[6] == 0:  # text blocks only
                        pdf_page.add_redact_annot(pymupdf.Rect(block[:4]), fill=False)
                pdf_page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE)

                azure_page = azure_pages.get(page_num)
                if not azure_page or not azure_page.words:
                    continue

                for word in azure_page.words:
                    xs = word.polygon[0::2]
                    ys = word.polygon[1::2]
                    rect = pymupdf.Rect(min(xs) * 72, min(ys) * 72,
                                        max(xs) * 72, max(ys) * 72)
                    fontsize = max(4, rect.height * 0.8)
                    pdf_page.insert_text((rect.x0, rect.y1), word.content,
                                         fontsize=fontsize, render_mode=3)

            scan.save(tmp_path, garbage=3, deflate=True)

        # Only reached if everything above worked. Swaps the file in one step.
        tmp_path.replace(path)
    finally:
        # If anything failed, the original is untouched. Just remove the leftover.
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def analyze(path):
    with open(path, "rb") as f:
        return client.begin_analyze_document("prebuilt-read", body=f).result()


def process_and_write(doc):
    # relpath is relative to the file's own root, so build the path from root_folder too
    path = file_path(doc)

    result = analyze(path)
    rows = get_page_confidence_scores(path, result)

    if fix_orientation(path, rows):           
        result = analyze(path)                
        rows = get_page_confidence_scores(path, result)

    db_rows = [{k: v for k, v in r.items() if k not in ("angle", "words")} for r in rows]
    save_pages(doc["uuid"], "doc_intel", db_rows)      # save_pages takes the file's uuid
    write_text_layer(path, result)
    update_ocr_service_intel(doc["file_id"])


def process_files():
    new_files = getDocsForIntel()
    if not new_files:
        print("no new files to process")
        return

    failed = []
    for f in new_files:
        try:
            process_and_write(f)
        except Exception:
            log.exception("Doc Intel failed for file_id=%s", f["file_id"])
            failed.append(f)

    if failed:
        print(f"{len(failed)} of {len(new_files)} files failed; see log")