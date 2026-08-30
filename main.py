import datetime
import os
import re
from tkinter import Tk, filedialog
import uuid
import cloudinary
import cloudinary.uploader
import easyocr
import mysql.connector
import certifi 
import cv2
import numpy as np
import warnings
from spellchecker import SpellChecker

warnings.filterwarnings("ignore", category=UserWarning)

# Load variables from .env into the process environment. Without this line,
# every os.getenv() below silently returns "" even if .env is filled in
# correctly — which is why Cloudinary uploads and TiDB writes were both
# failing with no visible error.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("python-dotenv not installed — run: pip install python-dotenv")

# ==============================================================================
# 1. CLOUD SERVICES CONFIGURATION
# ==============================================================================

# Cloudinary Configuration
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME", ""),
    api_key=os.getenv("CLOUDINARY_API_KEY", ""),
    api_secret=os.getenv("CLOUDINARY_API_SECRET", ""),
    secure=True,
)

# TiDB Cloud Configuration
TIDB_CONFIG = {
    "host": os.getenv("DB_HOST", ""),
    "port": int(os.getenv("DB_PORT", "4000")),
    "user": os.getenv("DB_USER", ""),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "legal_metrology_db"),
    "ssl_ca": certifi.where(),
    "ssl_verify_cert": True,
    "ssl_verify_identity": True,
}

# Initialize EasyOCR Reader (English) — load once, reuse for speed
print("Initializing EasyOCR Engine...")
reader = None
try:
    reader = easyocr.Reader(["en"], gpu=False, verbose=False, quantize=True)
except Exception as exc:
    print(f"EasyOCR initialization failed: {exc}")

# Lightweight dictionary check used only to detect gibberish OCR output
# (blurry/random photos), never for compliance rule matching itself.
_spell = SpellChecker(distance=1)
_spell.word_frequency.load_words([
    "mrp", "fssai", "pvt", "ltd", "kg", "kgs", "gm", "gms", "ml", "ltr",
    "mfg", "mfd", "pkd", "exp", "ist", "toll", "helpline", "mfr",
])


# ==============================================================================
# 2. IMAGE SELECTION & CLOUD STORAGE
# ==============================================================================

def pick_image_file():
    """Opens a native Windows file explorer to select any product image."""
    root = Tk()
    root.withdraw()
    root.attributes("-topmost", True)

    file_path = filedialog.askopenfilename(
        title="Select Product Label Image",
        filetypes=[
            ("Image Files", "*.jpg *.jpeg *.png *.bmp *.webp"),
            ("All Files", "*.*"),
        ],
    )
    root.destroy()
    return file_path


def upload_to_cloudinary(local_image_path):
    """Uploads the selected image to Cloudinary in the legal_metrology_scans folder."""
    print("\n[1/5] Uploading original image to Cloudinary...")
    try:
        response = cloudinary.uploader.upload(
            local_image_path,
            folder="legal_metrology_scans",
            timeout=10,
        )
        image_url = response.get("secure_url")
        print(f" -> Hosted Cloudinary URL: {image_url}")
        return image_url
    except Exception as err:
        print(f" -> Cloudinary Upload Failed: {err}")
        return None


# ==============================================================================
# 3. COMPUTER VISION & OCR TEXT EXTRACTION
# ==============================================================================

def optimize_image_for_ocr(image_path):
    """Multi-stage preprocessing for maximum OCR accuracy on packaging labels.

    Pipeline: auto-rotate → upscale → grayscale → denoise → sharpen →
    CLAHE → deskew → adaptive threshold → save variants.
    """
    print("[2/5] Enhancing image for AI vision (OpenCV)...")

    # --- Orientation fix -------------------------------------------------
    # The old code guessed rotation purely from aspect ratio (h > w*1.2 ->
    # rotate 90 CW). That fails for two reasons: (1) it can't distinguish
    # 90 CW from 90 CCW, so it's a coin flip on portrait photos, and
    # (2) most phone photos are already upright but carry an EXIF
    # orientation tag that cv2.imread ignores, so a phone photo taken
    # sideways-but-tagged-upright was never corrected at all.
    #
    # Fix: read with PIL first and apply ImageOps.exif_transpose(), which
    # respects the camera's EXIF orientation tag (this alone fixes the
    # common case). Then convert to OpenCV/BGR for the rest of the
    # pipeline. We no longer guess rotation from aspect ratio at all —
    # a correctly EXIF-rotated photo should already be upright; guessing
    # further on top of that does more harm than good.
    try:
        from PIL import Image, ImageOps
        pil_img = Image.open(image_path)
        pil_img = ImageOps.exif_transpose(pil_img)
        img = cv2.cvtColor(np.array(pil_img.convert("RGB")), cv2.COLOR_RGB2BGR)
    except Exception:
        img = cv2.imread(image_path)

    if img is None:
        raise ValueError(f"Unable to read the uploaded image: {image_path}")

    # Upscale for tiny print (higher scale for small images)
    max_dim = max(img.shape[:2])
    if max_dim < 800:
        scale = 2.5
    elif max_dim < 1200:
        scale = 1.8
    elif max_dim < 2000:
        scale = 1.3
    else:
        scale = 1.0
    if scale != 1.0:
        img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # --- Glare / shine fix (aluminum foil, plastic wrap, curved surfaces) ---
    # Shiny packaging produces small, very bright blown-out patches (specular
    # highlights) that wipe out the text underneath. We flag pixels that are
    # blown out (V > 235) AND low-saturation (true white blowout) AND well
    # ABOVE the image's own median brightness — that last condition is what
    # keeps this from false-flagging a naturally bright white label
    # background (median V there is already ~255, so nothing exceeds
    # "median + 40"). Verified: this correctly returns glare_ratio ≈ 0 on a
    # plain white label and correctly detects a real glare streak on a
    # metallic/gray background.
    #
    # IMPORTANT (tested, not assumed): inpainting can only recover text that
    # is partially or lightly clipped. Where glare has 100% blown out a
    # word's pixels, there is no information left to reconstruct — real OCR
    # tests on synthetic foil glare confirmed this: mild glare recovers
    # fully, heavy glare recovers surrounding text but not the fully-wiped
    # word. So we report glare_ratio back to the caller, which feeds the
    # quality gate below — a heavily glared photo should tell the user to
    # retake it rather than silently return a false "field not detected".
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    v_channel = hsv[:, :, 2].astype(np.int32)
    s_channel = hsv[:, :, 1]
    median_v = float(np.median(v_channel))
    glare_mask = (
        (v_channel > 235) & (v_channel > median_v + 40) & (s_channel < 30)
    ).astype(np.uint8) * 255
    glare_ratio = float(np.count_nonzero(glare_mask)) / glare_mask.size
    if 0 < glare_ratio < 0.35:
        glare_mask = cv2.dilate(glare_mask, np.ones((5, 5), np.uint8), iterations=1)
        img = cv2.inpaint(img, glare_mask, 5, cv2.INPAINT_TELEA)

    # Grayscale
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # Fast edge-preserving denoising
    denoised = cv2.fastNlMeansDenoising(gray, h=8, templateWindowSize=7, searchWindowSize=21)

    # Sharpening for crisp text edges
    sharpen_kernel = np.array([[-1, -1, -1], [-1, 9, -1], [-1, -1, -1]], dtype=np.float32)
    sharpened = cv2.filter2D(denoised, -1, sharpen_kernel)

    # CLAHE for local contrast (handles uneven lighting on shiny wrappers)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    contrast_enhanced = clahe.apply(sharpened)

    # Deskew (corrects slight rotation from camera/phone captures)
    coords = np.column_stack(np.where(contrast_enhanced < 128))
    if len(coords) > 100:
        angle = cv2.minAreaRect(coords)[-1]
        if angle < -45:
            angle = -(90 + angle)
        else:
            angle = -angle
        if abs(angle) > 0.5 and abs(angle) < 15:
            (hh, ww) = contrast_enhanced.shape[:2]
            center = (ww // 2, hh // 2)
            M = cv2.getRotationMatrix2D(center, angle, 1.0)
            contrast_enhanced = cv2.warpAffine(
                contrast_enhanced, M, (ww, hh),
                flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
            )

    # Adaptive threshold for binary variant (good for stamps/dark text)
    adaptive = cv2.adaptiveThreshold(
        contrast_enhanced, 255,
        cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
        cv2.THRESH_BINARY, 31, 10,
    )

    # Otsu threshold as alternative
    _, otsu = cv2.threshold(contrast_enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # Save all variants
    temp_path = "temp_optimized.jpg"
    cv2.imwrite(temp_path, adaptive)

    temp_original_path = "temp_original.jpg"
    cv2.imwrite(temp_original_path, img)

    temp_path_high_contrast = "temp_optimized_high_contrast.jpg"
    cv2.imwrite(temp_path_high_contrast, contrast_enhanced)

    temp_otsu_path = "temp_otsu.jpg"
    cv2.imwrite(temp_otsu_path, otsu)

    return temp_path, glare_ratio


def _focus_label_region(image_path):
    """Previously this blindly cropped 8% off left/right and 5% off top/bottom
    of EVERY image before OCR. That crop is the root cause of text getting
    chopped mid-word (e.g. "XYZ" -> "2", "Made in India" -> "le in India")
    whenever a label's text runs close to the frame edge — which is common
    on real product photos. Fixed by not cropping blindly at all: we just
    return the original image untouched. If you want ROI focusing later,
    it needs to be driven by actual text-detection boxes, not a fixed
    percentage guess.
    """
    return image_path


def extract_ocr_text(optimized_image_path):
    """Extracts raw text using multi-variant OCR with smart merging.

    Runs EasyCLAHE-enhanced, binary, Otsu, and original variants in parallel,
    then merges unique results for maximum coverage.
    """
    print("[3/5] Extracting text using EasyOCR...")
    if reader is None:
        raise RuntimeError("EasyOCR is not available. Install the engine dependencies and retry.")

    original_path = "temp_original.jpg"
    fallback_path = "temp_optimized_high_contrast.jpg"
    otsu_path = "temp_otsu.jpg"
    roi_path = _focus_label_region(optimized_image_path)

    # All candidate image paths (priority: ROI first, then optimized variants)
    candidate_paths = [roi_path, optimized_image_path]
    if os.path.exists(otsu_path):
        candidate_paths.append(otsu_path)
    if os.path.exists(fallback_path):
        candidate_paths.append(fallback_path)
    if os.path.exists(original_path):
        candidate_paths.append(original_path)

    allowlist = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.,:/()@-+%&= '₹"
    seen_text = set()
    extracted_chunks = []

    for path in candidate_paths:
        if not os.path.exists(path):
            continue

        # Single fast pass per variant (faster than multiple mag_ratios)
        try:
            ocr_results = reader.readtext(
                path,
                detail=0,
                paragraph=True,
                mag_ratio=1.4,
                width_ths=0.7,
                allowlist=allowlist,
                decoder="greedy",
                min_size=10,
                low_text=0.25,
            )
        except Exception:
            continue

        for chunk in ocr_results:
            cleaned = _clean_ocr_chunk(chunk)
            if not cleaned or len(cleaned) < 2:
                continue
            lower_cleaned = cleaned.lower()
            if lower_cleaned in seen_text:
                continue
            seen_text.add(lower_cleaned)
            extracted_chunks.append(cleaned)

        # The ROI normally contains all declarations; avoid expensive duplicate passes.
        if path == roi_path and len(extracted_chunks) >= 5:
            break

    raw_text = " ".join(extracted_chunks)
    if not raw_text:
        raw_text = " "

    # Clean up temp files
    for temp_path in (optimized_image_path, original_path, fallback_path, otsu_path, roi_path):
        if os.path.exists(temp_path):
            os.remove(temp_path)

    return raw_text


def _clean_ocr_chunk(text):
    """Cleans a single OCR text chunk: normalize spaces, fix common errors."""
    text = re.sub(r"\s+", " ", str(text)).strip()
    # Fix common OCR errors in Indian packaging labels
    text = re.sub(r"(?i)\b(MRP|M\.R\.P)\s*[:\-]?\s*Rs\.?\s*", "MRP Rs. ", text)
    text = re.sub(r"(?i)\b(NET)\s*(QTY|WT|WEIGHT|VOL|VOLUME)\b", r"NET \2", text)
    text = re.sub(r"(?i)\b(MFG|MFD)\s*[:\-]?\s*", "MFG ", text)
    text = re.sub(r"(?i)\b(PKD)\s*[:\-]?\s*", "PKD ", text)
    # Normalize currency symbols
    text = text.replace("₹", "Rs. ")
    text = re.sub(r"(?i)\bRS\.?\s*(\d)", r"Rs. \1", text)
    # Remove stray characters
    text = re.sub(r"[^A-Za-z0-9.,:/()@ \-\+%&=₹']", "", text)
    return text.strip()


def assess_text_quality(raw_text, min_tokens=3, real_word_threshold=0.35, glare_ratio=None,
                         glare_threshold=0.12):
    """
    Decides whether OCR text looks like a real label or noise from a
    blurry/dark/unreadable photo. This is what was missing before: bad
    photos were going straight into analyze_compliance() and coming back
    as a fake, misleading NON_COMPLIANT / 0% score, polluting the audit
    history with results that reflect a bad photo, not a bad product.

    real_word_ratio = fraction of alphabetic tokens (3+ letters) that are
    recognizable English words or whitelisted packaging terms (MRP, FSSAI,
    kg, etc). Below `real_word_threshold`, the text is flagged low quality.

    glare_ratio (optional) = fraction of the image that was detected as
    blown-out specular highlight by optimize_image_for_ocr(). This catches
    a case the text check alone misses: a shiny/foil photo can still leave
    behind a few real, spell-check-passing words (e.g. "NET WT 500g") even
    though the glare wiped out a critical declaration like "MADE IN INDIA"
    elsewhere on the same label. Tested against synthetic foil-glare images:
    inpainting recovers lightly-clipped text but cannot reconstruct fully
    blown-out pixels — real_word_ratio alone would pass that scan and the
    rule engine would then report a false "not detected" for the wiped
    field. Flagging it here instead tells the user to retake the photo.
    """
    tokens = re.findall(r"[A-Za-z]{3,}", raw_text or "")

    if len(tokens) < min_tokens:
        return {"is_low_quality": True, "reason": "too_little_text", "real_word_ratio": 0.0,
                "glare_ratio": glare_ratio}

    lowered = [t.lower() for t in tokens]
    unknown = _spell.unknown(lowered)
    known_ratio = round(1 - (len(unknown) / len(lowered)), 2)

    is_low_quality = known_ratio < real_word_threshold
    reason = "gibberish_text" if is_low_quality else None

    if not is_low_quality and glare_ratio is not None and glare_ratio >= glare_threshold:
        is_low_quality = True
        reason = "excessive_glare"

    return {
        "is_low_quality": is_low_quality,
        "reason": reason,
        "real_word_ratio": known_ratio,
        "glare_ratio": glare_ratio,
    }


# ==============================================================================
# 4. LEGAL METROLOGY COMPLIANCE RULE ENGINE
# ==============================================================================

def extract_entities(raw_text):
    """Extracts structured entities from OCR text for API response."""
    return {
        "commodity_name": _extract_commodity_name(raw_text),
        "net_quantity": _extract_net_quantity(raw_text),
        "mrp": _extract_mrp(raw_text),
        "date_declaration": _extract_date_declaration(raw_text),
        "manufacturer_details": _extract_manufacturer(raw_text),
        "country_of_origin": _extract_country_of_origin(raw_text),
        "consumer_care": _extract_consumer_care(raw_text),
    }


def _extract_commodity_name(text):
    """Extracts product/commodity name from label text."""
    text_upper = text.upper()
    product_match = re.search(
        r"\b([A-Z][A-Z\s&\-']{3,40}(?:OIL|POWDER|FLOUR|RICE|DAL|TEA|COOKIE|BISCUIT|MIX|SYRUP|JUICE|MILK|BREAD|NOODLE|PASTA|SAUCE|PICKLE|MASALA|SPICE|SALT|SUGAR|HONEY|JAM|CEREAL|ATTA|MAIDA|BESAN|SOYA|CORN|WHEAT|GRAM|LENTIL|NUT|NUTS|ALMOND|ALMONDS|BUTTER|GHEE|CREAM|CHEESE|YOGHURT|CUSTARD|SUJI|RAVA|SEMOLINA|VERMICELLI|CHIPS|NAMKEEN|SNACK|CHOCOLATE|TOFFEE|CANDY|WAFER))\b",
        text_upper,
    )
    if product_match:
        name = product_match.group(1).strip()
        name = re.split(
            r"\s+(?:NET|QTY|MRP|MFG|MFD|PKD|DATE|MADE|CONSUMER)\b",
            name,
            maxsplit=1,
            flags=re.IGNORECASE,
        )[0].strip()
        return name.title()
    label_text = re.split(
        r"\s+(?:NET|QTY|MRP|MFG|MFD|PKD|DATE|MADE|CONSUMER)\b",
        text,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    brand_match = re.search(
        r"\b([A-Z][A-Za-z&\-\s]{2,15}(?:'S)?)\s+([A-Z][A-Za-z&\-\s]{2,20})\b",
        label_text,
    )
    if brand_match:
        return f"{brand_match.group(1).strip()} {brand_match.group(2).strip()}"
    return "Not detected"


def _extract_net_quantity(text):
    """Extracts net quantity with value and unit."""
    text_upper = text.upper()
    patterns = [
        r"NET\s*(?:QTY|QUANTITY|WT|WEIGHT|VOL|VOLUME|CONT(?:ENT)?S?)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(G|GM|GMS|KG|KGS|ML|L|LTR|LTRS|LITRE|MG|UNITS|PIECES|U|N|PCS)",
        r"\b(\d+(?:\.\d+)?)\s*(G|GM|GMS|KG|KGS|ML|L|LTR|LTRS|LITRE|MG|UNITS|PIECES|U|N|PCS)\b",
    ]
    unit_map = {"G": "g", "GM": "g", "GMS": "g", "KG": "kg", "KGS": "kg",
                "ML": "ml", "L": "l", "LTR": "l", "LTRS": "l", "LITRE": "l",
                "MG": "mg", "UNITS": "units", "PIECES": "pieces", "U": "units",
                "N": "units", "PCS": "pieces"}
    for pattern in patterns:
        match = re.search(pattern, text_upper)
        if match:
            value = float(match.group(1))
            unit = match.group(2).upper()
            normalized = unit_map.get(unit, unit.lower())
            is_valid = normalized in {"g", "kg", "mg", "ml", "l", "units", "pieces"}
            return {"raw_text": f"{int(value) if value == int(value) else value} {normalized}",
                    "value": value, "unit": normalized, "is_valid_metric_unit": is_valid}
    return {"raw_text": "Not detected", "value": None, "unit": None, "is_valid_metric_unit": False}


def _extract_mrp(text):
    """Extracts MRP amount."""
    text_upper = text.upper()
    patterns = [
        r"MRP\s*(?:RS\.?|₹|INR|RUPEES)?\s*[:\-]?\s*(\d+(?:\.\d+)?)",
        r"M\.R\.P\s*(?:RS\.?|₹|INR)?\s*[:\-]?\s*(\d+(?:\.\d+)?)",
        r"MAX(?:IMUM)?\s*RETAIL\s*PRICE\s*[:\-]?\s*(\d+(?:\.\d+)?)",
        r"₹\s*(\d+(?:\.\d+)?)",
        r"RS\.?\s*(\d+(?:\.\d+)?)\s*(?:INCL(?:USIVE)?\s*OF\s*ALL\s*TAX(?:ES)?|/-)?",
        r"INR\s*(\d+(?:\.\d+)?)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text_upper)
        if match:
            amount = float(match.group(1))
            incl_taxes = bool(re.search(r"INCL(?:USIVE)?\s*OF\s*(?:ALL\s*)?TAX(?:ES)?", text_upper))
            return {"raw_text": match.group(0).strip(), "amount": amount,
                    "formatted_correctly": amount > 0, "inclusive_of_all_taxes": incl_taxes}
    return {"raw_text": "Not detected", "amount": None, "formatted_correctly": False, "inclusive_of_all_taxes": False}


def _extract_date_declaration(text):
    """Extracts manufacturing/packing/expiry date."""
    text_upper = text.upper()
    patterns = [
        (r"MFG\s*(?:DATE|DT)?\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "MANUFACTURE"),
        (r"MFD\s*(?:DATE|DT)?\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "MANUFACTURE"),
        (r"PKD\s*(?:DATE|DT)?\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "PACKING"),
        (r"PACKED\s*(?:ON|DATE)?\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "PACKING"),
        (r"USE\s*BY\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "EXPIRY"),
        (r"BEST\s*BEFORE\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "EXPIRY"),
        (r"EXP(?:IRY)?\s*(?:DATE)?\s*[:\-]?\s*(\d{1,2}[\/\-\.]\d{2,4}|\w+\s*\d{4})", "EXPIRY"),
    ]
    for pattern, date_type in patterns:
        match = re.search(pattern, text_upper)
        if match:
            raw_date = match.group(1)
            month, year = None, None
            parts = re.split(r"[\/\-\.]", raw_date)
            if len(parts) == 2:
                month, year = int(parts[0]), int(parts[1])
                if year < 100:
                    year += 2000
            elif len(parts) == 3:
                month, year = int(parts[1]), int(parts[2])
                if year < 100:
                    year += 2000
            return {"raw_text": match.group(0).strip(), "type": date_type,
                    "month": month, "year": year, "is_present": True}
    return {"raw_text": "Not detected", "type": None, "month": None, "year": None, "is_present": False}


def _extract_manufacturer(text):
    """Extracts manufacturer/packer name and address."""
    text_upper = text.upper()
    lines = text_upper.split("\n") if "\n" in text_upper else text_upper.split(" ")
    indicators = ["MFD", "MANUFACTURED", "PACKED", "MARKETED", "IMPORTED",
                  "PVT", "LTD", "LIMITED", "FSSAI", "ADDRESS"]
    mfg_lines = [l.strip() for l in lines if any(ind in l for ind in indicators)]
    if mfg_lines:
        raw = " ".join(mfg_lines)
        has_pincode = bool(re.search(r"\b\d{6}\b", raw))
        has_city = bool(re.search(r"\b(?:NAGAR|ROAD|ESTATE|INDUSTRAL|AREA|PHASE|VILLAGE|TALUKA|DISTRICT|STATE)\b", raw))
        return {"raw_text": raw[:200], "is_address_complete": has_pincode and has_city}
    return {"raw_text": "Not detected", "is_address_complete": False}


def _extract_country_of_origin(text):
    """Extracts country of origin for imports."""
    text_upper = text.upper()
    match = re.search(r"MADE\s*IN\s*(\w+)", text_upper)
    if match:
        return {"raw_text": match.group(0), "country": match.group(1), "is_declared": True}
    match = re.search(r"COUNTRY\s*OF\s*ORIGIN\s*[:\-]?\s*(\w+)", text_upper)
    if match:
        return {"raw_text": match.group(0), "country": match.group(1), "is_declared": True}
    match = re.search(r"PRODUCT\s*OF\s*(\w+)", text_upper)
    if match:
        return {"raw_text": match.group(0), "country": match.group(1), "is_declared": True}
    return {"raw_text": "Not detected", "country": None, "is_declared": False}


def _extract_consumer_care(text):
    """Extracts consumer care contact details."""
    text_upper = text.upper()
    care_lines = []
    phone_match = re.search(r"\b(?:1800[\-\s]?\d{3,6}[\-\s]?\d{3,6}|\d{10}|\d{6,8})\b", text)
    if phone_match:
        care_lines.append(phone_match.group(0))
    email_match = re.search(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b", text)
    if email_match:
        care_lines.append(email_match.group(0))
    web_match = re.search(r"\b(?:www\.|HTTPS?:\/\/)\S+", text_upper)
    if web_match:
        care_lines.append(web_match.group(0))
    if care_lines:
        return {"raw_text": ", ".join(care_lines), "is_present": True}
    care_keywords = ["CUSTOMER CARE", "CONSUMER CARE", "HELPLINE", "TOLL FREE",
                     "FEEDBACK", "COMPLAINT", "WRITE TO", "CONTACT"]
    for keyword in care_keywords:
        if keyword in text_upper:
            return {"raw_text": keyword, "is_present": True}
    return {"raw_text": "Not detected", "is_present": False}


def analyze_compliance(raw_text):
    """Evaluates extracted text against Legal Metrology (Packaged Commodities) Rules, 2011."""
    print("[4/5] Running Legal Metrology Rule Analysis...")
    text_upper = raw_text.upper()

    checks = {
        "manufacturer_address": False,
        "commodity_name": False,
        "net_quantity": False,
        "date_manufacture": False,
        "mrp": False,
        "unit_sale_price": False,
        "country_of_origin": False,
        "consumer_care": False,
    }

    violations = []

    # 1. Manufacturer/Packer Name & Address - Rule 6(1)(a)
    mfg_patterns = [
        r"MFD\s*BY", r"MANUFACTURED\s*(BY|FOR)", r"PACKED\s*BY", r"PKD\s*BY",
        r"MARKETED\s*(BY|FOR)", r"IMPORTED\s*(BY|FOR)", r"MANUFACTURER",
        r"PVT\.?\s*LTD", r"LIMITED", r"FSSAI", r"LIC(?:ENSE)?\s*NO",
        r"ADDRESS", r"ESTATE", r"NAGAR", r"ROAD", r"INDUSTRIAL\s*(AREA|ESTATE)",
        r"VILLAGE", r"TALUKA", r"DISTRICT", r"STATE", r"PIN\s*\d{6}",
    ]
    if any(re.search(p, text_upper) for p in mfg_patterns):
        checks["manufacturer_address"] = True
    else:
        violations.append({
            "rule": "Rule 6(1)(a)",
            "severity": "CRITICAL",
            "field": "Manufacturer/Packer Name & Address",
            "issue": "Name and address of manufacturer/packer not detected.",
            "remediation": "Print complete name and address of manufacturer/packer/importer.",
        })

    # 2. Commodity Name - Rule 6(1)(b)
    commodity_patterns = [
        r"(?:COMMODITY|PRODUCT|ITEM|FOOD|DESCRIPTION)\s*[:\-]?\s*[A-Z]",
        r"(?:BRAND|TRADE\s*NAME)\s*[:\-]?\s*[A-Z]",
        r"\b[A-Z][A-Z\s]{3,30}(?:OIL|POWDER|FLOUR|RICE|DAL|TEA|COOKIE|BISCUIT|MIX|SYRUP|JUICE|MILK|BREAD|NOODLE|PASTA|SAUCE|PICKLE|MASALA|SPICE|SALT|SUGAR|HONEY|JAM|KEETCHUP|KETCHUP)\b",
    ]
    if any(re.search(p, text_upper) for p in commodity_patterns):
        checks["commodity_name"] = True
    else:
        if len(raw_text.strip()) > 30:
            checks["commodity_name"] = True
        else:
            violations.append({
                "rule": "Rule 6(1)(b)",
                "severity": "WARNING",
                "field": "Name of Commodity",
                "issue": "Commodity name not clearly identified.",
                "remediation": "Declare the common name of the commodity on the label.",
            })

    # 3. Net Quantity - Rule 6(1)(c)
    qty_patterns = [
        r"NET\s*(QTY|QUANTITY|WT|WEIGHT|VOL|VOLUME|CONT(?:ENT)?S?)\s*[:\-]?\s*\d",
        r"\b\d+(?:\.\d+)?\s*(G|GM|GMS|KG|KGS|ML|L|LTR|LTRS|LITRE|LITRES|MG|MGMS|UNITS|PIECES|U|N|PCS|PC)\b",
        r"\b\d+(?:\.\d+)?\s*(GRAM|GRAMS|KILOGRAM|KG|MILLILITRE|LITRE)\b",
        r"NET\s*\d+\s*(G|KG|ML|L)\b",
        r"QTY\s*[:\-]?\s*\d+",
    ]
    if any(re.search(p, text_upper) for p in qty_patterns):
        checks["net_quantity"] = True
    else:
        violations.append({
            "rule": "Rule 6(1)(c)",
            "severity": "CRITICAL",
            "field": "Net Quantity",
            "issue": "Net quantity in standard SI units not detected.",
            "remediation": "Declare net quantity using standard units (g, kg, ml, l) with proper spacing.",
        })

    # 4. Month & Year of Manufacture/Packing - Rule 6(1)(d)
    date_patterns = [
        r"MFG\s*(DATE|DT)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"MFD\s*(DATE|DT)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"PKD\s*(DATE|DT)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"PACKED\s*(ON|DATE)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"MANUFACTURED\s*(ON|DATE)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"USE\s*BY|BEST\s*BEFORE|EXP(?:IRY)?\s*(DATE)?\s*[:\-]?\s*\d{1,2}[\/\-\.]\d{2,4}",
        r"BATCH\s*(NO|NUMBER)?\s*[:\-]?\s*\w+",
        r"\b\d{1,2}[\/\-\.](?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\b",
        r"\b(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s*\d{4}\b",
        r"\b\d{2}[\/\-\.]\d{4}\b",
    ]
    if any(re.search(p, text_upper) for p in date_patterns):
        checks["date_manufacture"] = True
    else:
        violations.append({
            "rule": "Rule 6(1)(d)",
            "severity": "CRITICAL",
            "field": "Month & Year of Manufacture/Packing",
            "issue": "Month/year of manufacture or packing not found.",
            "remediation": "Indicate month and year of manufacture, packing, or import.",
        })

    # 5. MRP (inclusive of all taxes) - Rule 6(1)(e)
    mrp_patterns = [
        r"MRP\s*(RS\.?|₹|INR|RUPEES)?\s*[:\-]?\s*\d+(?:\.\d+)?",
        r"M\.R\.P\s*(RS\.?|₹|INR)?\s*[:\-]?\s*\d+(?:\.\d+)?",
        r"MAX(?:IMUM)?\s*RETAIL\s*PRICE\s*[:\-]?\s*\d+(?:\.\d+)?",
        r"RS\.?\s*₹?\s*\d+(?:\.\d+)?\s*(?:INCL(?:USIVE)?\s*OF\s*ALL\s*TAX(?:ES)?)?",
        r"₹\s*\d+(?:\.\d+)?",
        r"INR\s*\d+(?:\.\d+)?",
        r"PRICE\s*[:\-]?\s*(?:RS\.?|₹)?\s*\d+(?:\.\d+)?",
    ]
    if any(re.search(p, text_upper) for p in mrp_patterns):
        checks["mrp"] = True
    else:
        violations.append({
            "rule": "Rule 6(1)(e)",
            "severity": "CRITICAL",
            "field": "Maximum Retail Price (MRP)",
            "issue": "MRP declaration missing or unreadable.",
            "remediation": "Print MRP (inclusive of all taxes) clearly on the label.",
        })

    # 6. Unit Sale Price - Rule 6(1)(f)
    usp_patterns = [
        r"(?:PER|@)\s*(?:RS\.?|₹)?\s*\d+(?:\.\d+)?\s*(?:PER\s*)?(G|GM|KG|ML|L|LTR|GRAM|KILOGRAM|LITRE|PIECE|UNIT)",
        r"\d+(?:\.\d+)?\s*(?:RS\.?|₹|PAISE?)\s*PER\s*(G|GM|KG|ML|L|LTR|GRAM|KILOGRAM|LITRE|PIECE|UNIT)",
        r"UNIT\s*(?:SALE\s*)?PRICE\s*[:\-]?\s*\d+(?:\.\d+)?",
        r"RATE\s*[:\-]?\s*\d+(?:\.\d+)?\s*PER\s*(G|GM|KG|ML|L|LTR)",
    ]
    if any(re.search(p, text_upper) for p in usp_patterns):
        checks["unit_sale_price"] = True
    else:
        violations.append({
            "rule": "Rule 6(1)(f)",
            "severity": "WARNING",
            "field": "Unit Sale Price",
            "issue": "Unit sale price (price per unit weight/volume) not detected.",
            "remediation": "Declare unit sale price where applicable (e.g., Rs. per 100g or per kg).",
        })

    # 7. Country of Origin - Rule 6(1)(g)
    origin_patterns = [
        r"MADE\s*IN\s*(INDIA|IND)",
        r"COUNTRY\s*OF\s*ORIGIN\s*[:\-]?\s*\w+",
        r"ORIGIN\s*[:\-]?\s*\w+",
        r"PRODUCT\s*OF\s*\w+",
        r"IMPORTED\s*(BY|FROM)",
    ]
    if any(re.search(p, text_upper) for p in origin_patterns):
        checks["country_of_origin"] = True
    else:
        import_indicators = re.search(r"IMPORT|FOREIGN|COUNTRY\s*OF\s*ORIGIN", text_upper)
        if import_indicators:
            violations.append({
                "rule": "Rule 6(1)(g)",
                "severity": "CRITICAL",
                "field": "Country of Origin",
                "issue": "Imported product missing country of origin declaration.",
                "remediation": "Declare the country of origin for imported packaged commodities.",
            })
        else:
            checks["country_of_origin"] = True

    # 8. Consumer Care Details - Rule 6(2)
    care_patterns = [
        r"CUSTOMER\s*CARE", r"CONSUMER\s*CARE", r"HELPLINE",
        r"TOLL\s*FREE", r"FEEDBACK", r"COMPLAINT", r"CARE\s*(NO|NUMBER|PHONE)",
        r"EMAIL\s*[:\-]?\s*\S+@\S+", r"@\s*\S+\.\S+",
        r"\b1800[\-\s]?\d{3,6}[\-\s]?\d{3,6}\b",
        r"\b\d{10}\b",
        r"\b\d{6,8}\b",
        r"WEBSITE\s*[:\-]?\s*\S+", r"WWW\.\S+",
        r"WRITE\s*TO", r"CONTACT\s*(US|NO|AT)",
    ]
    if any(re.search(p, text_upper) for p in care_patterns):
        checks["consumer_care"] = True
    else:
        violations.append({
            "rule": "Rule 6(2)",
            "severity": "WARNING",
            "field": "Consumer Care Details",
            "issue": "Consumer contact details / complaint helpline missing.",
            "remediation": "Provide contact phone number, email, or address for consumer complaints.",
        })

    # Scoring & Status Determination
    passed_checks = sum(1 for status in checks.values() if status)
    compliance_score = round((passed_checks / len(checks)) * 100, 2)

    if compliance_score == 100.0:
        overall_status = "COMPLIANT"
    elif compliance_score >= 75.0:
        overall_status = "WARNING"
    else:
        overall_status = "NON_COMPLIANT"

    return overall_status, compliance_score, checks, violations


# ==============================================================================
# 5. DATABASE PERSISTENCE (TiDB CLOUD)
# ==============================================================================

def _entities_to_field_rows(entities):
    """Flattens the extract_entities() dict into short (field_name, value,
    was_detected) rows for the `extracted_fields` table — this is what
    replaces dumping the whole raw OCR paragraph into one messy TEXT column.
    """
    rows = []

    commodity = entities.get("commodity_name") or "Not detected"
    rows.append(("commodity_name", commodity, commodity != "Not detected"))

    dict_fields = [
        ("net_quantity", entities.get("net_quantity")),
        ("mrp", entities.get("mrp")),
        ("date_declaration", entities.get("date_declaration")),
        ("manufacturer_details", entities.get("manufacturer_details")),
        ("country_of_origin", entities.get("country_of_origin")),
        ("consumer_care", entities.get("consumer_care")),
    ]
    for field_name, value in dict_fields:
        raw = (value or {}).get("raw_text", "Not detected")
        rows.append((field_name, raw[:255], raw != "Not detected"))

    return rows


def save_to_tidb(scan_id, timestamp_str, image_url, status, score, entities, violations,
                  is_low_quality=False, real_word_ratio=None, glare_ratio=None,
                  quality_reason=None):
    """Inserts scan metadata into 'scans', short structured declarations into
    'extracted_fields', and individual errors into 'violations'.

    No more raw OCR text blob: instead of one long messy paragraph per scan,
    each declaration (commodity name, MRP, net quantity, etc.) gets its own
    short row via _entities_to_field_rows(). Much easier to read/query when
    you have 100+ scans in the history.

    Run migration.sql once against your database first — it drops every old/
    messy table (typo'd names, orphaned tables, the old raw_ocr_text column)
    and creates the clean schema this function expects.
    """
    print("[5/5] Writing report to TiDB Cloud Database...")
    conn = None
    try:
        conn = mysql.connector.connect(**TIDB_CONFIG)
        cursor = conn.cursor()

        # Insert Scan Record
        insert_scan_query = """
        INSERT INTO scans (scan_id, timestamp, image_path, overall_status, compliance_score,
                            is_low_quality, quality_reason, real_word_ratio, glare_ratio)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        scan_data = (scan_id, timestamp_str, image_url, status, score,
                     is_low_quality, quality_reason, real_word_ratio, glare_ratio)
        cursor.execute(insert_scan_query, scan_data)

        # Insert extracted-field records (short, one per declaration)
        insert_field_query = """
        INSERT INTO extracted_fields (scan_id, field_name, field_value, was_detected)
        VALUES (%s, %s, %s, %s)
        """
        for field_name, field_value, was_detected in _entities_to_field_rows(entities):
            cursor.execute(insert_field_query, (scan_id, field_name, field_value, was_detected))

        # Insert Violation Records
        if violations:
            insert_violation_query = """
            INSERT INTO violations (scan_id, rule_reference, severity, field_name, issue_description, remediation)
            VALUES (%s, %s, %s, %s, %s, %s)
            """
            for v in violations:
                violation_data = (
                    scan_id, v["rule"], v["severity"], v["field"], v["issue"], v["remediation"]
                )
                cursor.execute(insert_violation_query, violation_data)

        conn.commit()
        print(" -> SUCCESS: Scan, extracted fields, and violations saved to TiDB Cloud tables!")

        cursor.close()
    except Exception as err:
        print(f" -> Database Persistence Error: {err}")
    finally:
        if conn and conn.is_connected():
            conn.close()


# ==============================================================================
# 6. PIPELINE EXECUTION
# ==============================================================================

def main():
    selected_image = pick_image_file()
    if not selected_image:
        print("No image selected. Process aborted.")
        return

    # Metadata generation
    scan_id = str(uuid.uuid4())
    current_time = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 1. Cloudinary upload (Uploads original for dashboard viewing)
    hosted_image_url = upload_to_cloudinary(selected_image)

    # 2. Computer Vision Optimization (Rotates and enhances contrast)
    optimized_img_path, glare_ratio = optimize_image_for_ocr(selected_image)

    # 3. Text extraction (Reads the optimized image)
    raw_text = extract_ocr_text(optimized_img_path)

    # 4. Text quality gate — catches blurry/unreadable photos before they
    #    get scored as a fake compliance failure. glare_ratio is passed in
    #    so a heavily glared foil/plastic photo is caught even if a few
    #    unaffected words still passed the spell-check ratio.
    quality = assess_text_quality(raw_text, glare_ratio=glare_ratio)

    if quality["is_low_quality"]:
        status, score = "LOW_QUALITY_IMAGE", None
        checks = {
            "manufacturer_address": False, "commodity_name": False, "net_quantity": False,
            "date_manufacture": False, "mrp": False, "unit_sale_price": False,
            "country_of_origin": False, "consumer_care": False,
        }
        if quality["reason"] == "excessive_glare":
            issue_text = "Strong glare/shine (common on foil or curved plastic packs) is covering part of the label, so some declarations can't be reliably read."
            remediation_text = "Retake the photo at a slight angle away from direct light, or diffuse the light source, so the shiny surface doesn't blow out the text."
        else:
            issue_text = "The photo is too blurry, dark, or unclear for reliable text extraction."
            remediation_text = "Retake the photo in good lighting, holding the camera steady and close to the label."
        violations = [{
            "rule": "N/A", "severity": "CRITICAL", "field": "Image Quality",
            "issue": issue_text,
            "remediation": remediation_text,
        }]
        entities = extract_entities("")
        print(f"[4/5] Skipping rule analysis — image quality too low to trust the text ({quality['reason']}).")
    else:
        # 4. Rule engine
        status, score, checks, violations = analyze_compliance(raw_text)
        # 4b. Entity extraction for display
        entities = extract_entities(raw_text)

    # 5. TiDB Storage — stores structured entities, not the raw OCR blob
    save_to_tidb(scan_id, current_time, hosted_image_url, status, score, entities, violations,
                 is_low_quality=quality["is_low_quality"], real_word_ratio=quality["real_word_ratio"],
                 glare_ratio=quality.get("glare_ratio"), quality_reason=quality.get("reason"))

    # Terminal / IDLE Display
    print("\n" + "=" * 65)
    print(f"AUDIT SUMMARY (Scan ID: {scan_id})")
    print("=" * 65)
    print(f"Timestamp        : {current_time}")
    print(f"Cloudinary URL   : {hosted_image_url}")
    print(f"Overall Status   : {status}")
    print(f"Compliance Score : {score if score is not None else 'N/A'}%")
    print(f"Text Quality     : real_word_ratio={quality['real_word_ratio']} "
          f"({'LOW QUALITY' if quality['is_low_quality'] else 'OK'})")
    print("-" * 65)

    print("EXTRACTED DECLARATIONS:")
    print(f" • Commodity      : {entities['commodity_name']}")
    print(f" • Net Quantity   : {entities['net_quantity']['raw_text']}")
    print(f" • MRP            : {entities['mrp']['raw_text']}")
    print(f" • MFG/PKD Date   : {entities['date_declaration']['raw_text']}")
    print(f" • Manufacturer   : {entities['manufacturer_details']['raw_text'][:60]}")
    print(f" • Origin         : {entities['country_of_origin']['raw_text']}")
    print(f" • Consumer Care  : {entities['consumer_care']['raw_text']}")

    print("-" * 65)
    print("MANDATORY RULE CHECKS:")
    for rule, passed in checks.items():
        print(f" • {rule.replace('_', ' ').upper():<20}: {'[PASS]' if passed else '[FAIL]'}")

    print("-" * 65)
    print("RECORDED VIOLATIONS:")
    if not violations:
        print(" • No violations detected. Product is fully compliant.")
    else:
        for idx, item in enumerate(violations, start=1):
            print(f" {idx}. [{item['severity']}] {item['field']} ({item['rule']})[cite: 1]")
            print(f"    Issue      : {item['issue']}")
            print(f"    Remediation: {item['remediation']}")
    print("=" * 65 + "\n")


if __name__ == "__main__":
    main()