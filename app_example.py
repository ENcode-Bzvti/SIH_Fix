import os
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, request, send_from_directory
from werkzeug.utils import secure_filename

from main import (
    analyze_compliance,
    assess_text_quality,
    extract_ocr_text,
    optimize_image_for_ocr,
    upload_to_cloudinary,
    extract_entities,
    save_to_tidb,
    TIDB_CONFIG,
)

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp", "bmp"}

# Persistence only turns on once every DB_* variable is actually set —
# same behaviour the README already promised, now actually implemented.
DB_CONFIGURED = all(TIDB_CONFIG.get(k) for k in ("host", "user", "password", "database"))
CLOUDINARY_ENABLED = os.environ.get("ENABLE_CLOUDINARY", "").lower() in {"1", "true", "yes"}

print(f"[startup] TiDB persistence : {'ON' if DB_CONFIGURED else 'OFF — check .env DB_HOST/DB_USER/DB_PASSWORD/DB_NAME'}")
print(f"[startup] Cloudinary upload: {'ON' if CLOUDINARY_ENABLED else 'OFF — set ENABLE_CLOUDINARY=true in .env'}")


def _is_allowed_file(filename: str) -> bool:
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def _build_scan_response(raw_text: str, file_name: str, image_url: Optional[str] = None,
                          glare_ratio: Optional[float] = None) -> Dict[str, Any]:
    quality = assess_text_quality(raw_text, glare_ratio=glare_ratio)

    if quality["is_low_quality"]:
        # Don't run the rule engine on noise — it would produce a fake,
        # misleading NON_COMPLIANT score. Be honest that the photo, not
        # the product, is the problem.
        overall_status, compliance_score, checks = "LOW_QUALITY_IMAGE", None, {}
        if quality["reason"] == "excessive_glare":
            issue_text = "Strong glare/shine (common on foil or curved plastic packs) is covering part of the label, so some declarations can't be reliably read."
            remediation_text = "Retake the photo at a slight angle away from direct light, or diffuse the light source, so the shiny surface doesn't blow out the text."
            summary = "Glare on the packaging is blocking part of the label — please retake the photo."
        else:
            issue_text = "The photo is too blurry, dark, or unclear for reliable text extraction."
            remediation_text = "Retake the photo in good lighting, holding the camera steady and close to the label."
            summary = "The uploaded photo could not be read reliably — please retake it in better lighting."
        violations = [{
            "rule": "N/A",
            "severity": "CRITICAL",
            "field": "Image Quality",
            "issue": issue_text,
            "remediation": remediation_text,
        }]
        extracted_entities: Dict[str, Any] = {}
    else:
        overall_status, compliance_score, checks, violations = analyze_compliance(raw_text)
        extracted_entities = extract_entities(raw_text)
        summary = "No critical compliance issues detected."
        if violations:
            critical = [v for v in violations if v["severity"].upper() in {"CRITICAL", "WARNING"}]
            summary = f"The scanner identified {len(critical)} compliance issue(s) requiring review." if critical else "Routine review recommended."

    violation_list: List[Dict[str, str]] = []
    for item in violations:
        violation_list.append(
            {
                "rule_reference": item["rule"],
                "severity": item["severity"],
                "field": item["field"],
                "issue": item["issue"],
                "remediation": item["remediation"],
            }
        )

    return {
        "audit_metadata": {
            "scan_id": f"scan-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}-{os.urandom(4).hex()}",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "overall_status": overall_status,
            "compliance_score_percentage": round(float(compliance_score), 2) if compliance_score is not None else None,
            "source_file": file_name,
            "image_url": image_url,
        },
        "extracted_entities": extracted_entities,
        "violations_found": violation_list,
        "ai_summary": summary,
        "checks": checks,
        "legal_references": {
            "act": "Legal Metrology Act, 2009",
            "rules": "Legal Metrology (Packaged Commodities) Rules, 2011",
            "official_source": "https://consumeraffairs.gov.in/pages/legal-metrology-act",
            "disclaimer": "OCR results are an assistive screening and require human verification.",
        },
        "raw_ocr_text": raw_text,
        "is_low_quality": quality["is_low_quality"],
        "quality_reason": quality["reason"],
        "real_word_ratio": quality["real_word_ratio"],
        "glare_ratio": quality.get("glare_ratio"),
    }


@app.get("/")
def home():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/health")
def health_check():
    return jsonify({"status": "ok"})


@app.get("/health/db")
def health_check_db():
    if not DB_CONFIGURED:
        return jsonify({"status": "not_configured",
                         "message": "Set DB_HOST, DB_USER, DB_PASSWORD, DB_NAME in .env to enable persistence."}), 200
    try:
        import mysql.connector
        conn = mysql.connector.connect(**TIDB_CONFIG)
        conn.close()
        return jsonify({"status": "ok"})
    except Exception as exc:
        return jsonify({"status": "error", "details": str(exc)}), 500


@app.post("/api/scan")
@app.post("/api/v1/scans")
def scan_label():
    source_field = "label_image" if "label_image" in request.files else "file"
    if source_field not in request.files:
        return jsonify({"error": "No image file found in the request."}), 400

    uploaded_file = request.files[source_field]
    if uploaded_file.filename == "":
        return jsonify({"error": "No file selected."}), 400

    if not _is_allowed_file(uploaded_file.filename):
        return jsonify({"error": "Unsupported file type. Please upload a PNG, JPG, JPEG, WEBP or BMP image."}), 400

    safe_name = secure_filename(uploaded_file.filename)
    destination = UPLOAD_DIR / safe_name
    uploaded_file.save(destination)

    try:
        optimized_path, glare_ratio = optimize_image_for_ocr(str(destination))
        raw_text = extract_ocr_text(optimized_path)
        image_url = None
        if CLOUDINARY_ENABLED:
            image_url = upload_to_cloudinary(str(destination))
        response = _build_scan_response(raw_text, safe_name, image_url=image_url, glare_ratio=glare_ratio)

        if DB_CONFIGURED:
            try:
                meta = response["audit_metadata"]
                db_timestamp = datetime.fromisoformat(meta["timestamp"]).strftime("%Y-%m-%d %H:%M:%S")
                db_violations = [
                    {"rule": v["rule_reference"], "severity": v["severity"], "field": v["field"],
                     "issue": v["issue"], "remediation": v["remediation"]}
                    for v in response["violations_found"]
                ]
                save_to_tidb(
                    meta["scan_id"], db_timestamp, image_url,
                    meta["overall_status"], meta["compliance_score_percentage"],
                    response["extracted_entities"], db_violations,
                    is_low_quality=response["is_low_quality"],
                    real_word_ratio=response["real_word_ratio"],
                    glare_ratio=response.get("glare_ratio"),
                    quality_reason=response.get("quality_reason"),
                )
            except Exception as db_err:
                # Never fail the scan just because persistence failed —
                # the user still gets their audit result either way.
                print(f" -> DB persistence skipped: {db_err}")

        return jsonify(response)
    except ValueError as exc:
        return jsonify({"error": "The uploaded file could not be read as a valid image.", "details": str(exc)}), 400
    except Exception as exc:  # pragma: no cover - backend error path
        return jsonify({"error": "AI compliance engine failed to process the uploaded label.", "details": str(exc)}), 500


def get_available_port(preferred_ports=(5000, 5001, 5002, 8000, 8001, 8080)):
    for port in preferred_ports:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
    return 5001


if __name__ == "__main__":
    configured_port = os.environ.get("PORT")
    port = int(configured_port) if configured_port and configured_port.isdigit() else get_available_port()
    if configured_port:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", port))
        except OSError:
            port = get_available_port()

    print(f"Starting Legal Metrology Scanner on http://127.0.0.1:{port}")
    app.run(host="0.0.0.0", port=port, debug=False)
