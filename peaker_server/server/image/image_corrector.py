
import cv2
import numpy as np
import pymupdf
from PIL import Image

try:  
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

MODEL = "u2netp"   
DPI = 150          
PAD = 10           
SKEW_SIGN = 1      

_session = None


def _get_session():
    """Load the background-removal model once and reuse it."""
    global _session
    if _session is None:
        from rembg import new_session
        _session = new_session(MODEL)
    return _session


def _text_line_angle(bgr):
    """Median skew from text lines (fallback when the page fills the frame)."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    blur = cv2.GaussianBlur(gray, (9, 9), 0)
    thresh = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (30, 5))
    dilate = cv2.dilate(thresh, kernel, iterations=2)
    contours, _ = cv2.findContours(dilate, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    angles = []
    for c in contours:
        if cv2.contourArea(c) > 100:
            a = cv2.minAreaRect(c)[-1] % 90
            if a > 45:
                a -= 90
            angles.append(a)
    return float(np.median(angles)) if angles else 0.0


def _page_mask(bgr):
    """White where the paper is, black where the scanner background is."""
    from rembg import remove
    rgb = Image.fromarray(cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB))
    mask = remove(rgb, session=_get_session(), only_mask=True)
    return (np.array(mask) > 128).astype(np.uint8) * 255


def _deskew_and_crop(bgr):
    h, w = bgr.shape[:2]
    mask = _page_mask(bgr)
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if contours:
        c = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(c)
        if area < 0.2 * w * h:               # mask looks wrong, don't trust it
            mask = np.full((h, w), 255, np.uint8)
            angle = _text_line_angle(bgr)
        elif area > 0.97 * w * h:            # page fills the frame
            angle = _text_line_angle(bgr)
        else:
            a = cv2.minAreaRect(c)[-1] % 90
            angle = a - 90 if a > 45 else a
    else:
        mask = np.full((h, w), 255, np.uint8)
        angle = _text_line_angle(bgr)

    angle *= SKEW_SIGN
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    rotated = cv2.warpAffine(bgr, M, (w, h), flags=cv2.INTER_CUBIC,
                             borderValue=(255, 255, 255))
    rot_mask = cv2.warpAffine(mask, M, (w, h), flags=cv2.INTER_NEAREST)

    ys, xs = np.where(rot_mask > 0)
    if len(xs) == 0:
        return rotated
    x0, x1 = max(xs.min() - PAD, 0), min(xs.max() + PAD, w)
    y0, y1 = max(ys.min() - PAD, 0), min(ys.max() + PAD, h)
    return rotated[y0:y1, x0:x1]


def clean_page(image):
    """Deskew and crop one scanned page. See module docstring for input types."""
    if isinstance(image, pymupdf.Page):
        pix = image.get_pixmap(dpi=DPI, colorspace=pymupdf.csRGB)
        arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, 3)
        return Image.fromarray(_deskew_and_crop(cv2.cvtColor(arr, cv2.COLOR_RGB2BGR))[:, :, ::-1])

    if isinstance(image, Image.Image):
        bgr = cv2.cvtColor(np.array(image.convert("RGB")), cv2.COLOR_RGB2BGR)
        return Image.fromarray(_deskew_and_crop(bgr)[:, :, ::-1])

    if isinstance(image, np.ndarray):
        return _deskew_and_crop(image)

    raise TypeError("Pass a PIL Image, a BGR numpy array, or a pymupdf Page.")