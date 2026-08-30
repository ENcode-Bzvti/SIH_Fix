# Legal Metrology Compliance Scanner

Flask web app for OCR-assisted checks of Indian packaged commodity labels.

## Run in VS Code

Open this folder in VS Code, create a terminal, and run:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python app_example.py
```

Then open the URL printed by Flask (normally
<http://127.0.0.1:5000>). For one-click startup on Windows, double-click
`start_app.bat`; it selects a free port and opens the browser after `/health`
responds.

Set real TiDB values in `.env`. The API still runs its OCR audit without a database; persistence is enabled automatically when all `DB_*` variables are configured. The first EasyOCR scan may download model files.

If Python reports `ModuleNotFoundError: No module named 'cloudinary'`, select the
workspace interpreter `.venv\Scripts\python.exe` in VS Code, then run:

```powershell
python -m pip install -r requirements.txt
```

## API

- `GET /health` - service health
- `GET /health/db` - TiDB connectivity (now actually implemented — returns `not_configured` if `.env` DB_* vars aren't set)
- `GET /` - scanner web interface
- `POST /api/scan` - multipart upload field named `label_image` (also accepts `file`)
- `POST /api/v1/scans` - compatibility route

Example:

```powershell
curl.exe -X POST http://127.0.0.1:5000/api/scan -F "label_image=@mock_label.png"
```

Results include OCR evidence, extracted declarations, rule references, violations, remediation, score, and an explicit human-verification flag. Font-size checks remain warnings unless a physical calibration reference is supplied.

The scanner now runs OCR against the original image plus upscaled, contrast-enhanced,
and adaptive-threshold views. This improves recognition of small or low-contrast
packaging text. Manufacturer checks recognize explicit manufactured/packed/marketed
by labels, importer and packer labels, name-and-address declarations, and company
suffixes without treating an `MFG Date` label as a manufacturer declaration.

## Text quality gate (new)

Blurry, dark, or random photos used to get scored as `NON_COMPLIANT 0%` — a
false result, since the *photo* failed, not the product. Every scan now
runs through `assess_text_quality()` first: it checks whether the OCR text
contains real recognizable words. If not, the response comes back as:

```json
{ "audit_metadata": { "overall_status": "LOW_QUALITY_IMAGE", "compliance_score_percentage": null }, ... }
```

The frontend shows this with a distinct dashed blue badge (not the red
"non-compliant" stamp) so users understand it's a "please retake the photo"
prompt, not a failed audit.

**Before persistence will work, run this once in your TiDB console:**
```sql
ALTER TABLE scans ADD COLUMN is_low_quality BOOLEAN DEFAULT FALSE;
ALTER TABLE scans ADD COLUMN real_word_ratio DECIMAL(4,2) DEFAULT NULL;
```
Without these two columns, `/api/scan` still works and returns results —
it just logs a "DB persistence skipped" message to the console and moves on.

## Security

Never commit `.env`. The database password previously shared in chat must be rotated in TiDB Cloud before use.
