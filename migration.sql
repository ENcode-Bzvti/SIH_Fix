-- ==============================================================================
-- Legal Metrology Compliance Scanner — clean database reset
-- Run this once in your TiDB / MySQL console. It DROPS every old/messy
-- table (including any typo'd or orphaned ones like `voilations`,
-- `compliance_scans`, etc.) and creates clean tables that match what
-- main.py actually writes to. Nothing else is needed — no more manual
-- ALTER TABLE steps after this.
--
-- Note: the old `raw_ocr_text` column (a single TEXT blob dumping every
-- word OCR found, unstructured) is GONE. Instead, each declaration the
-- scanner extracts (commodity name, MRP, net quantity, etc.) gets its own
-- clean row in `extracted_fields` — no more 100-word wall of text per scan.
-- ==============================================================================

-- 1) Wipe everything old. Add any other stray table names you see in
--    `SHOW TABLES;` to this list before running.
DROP TABLE IF EXISTS violations;
DROP TABLE IF EXISTS voilations;
DROP TABLE IF EXISTS extracted_fields;
DROP TABLE IF EXISTS scans;
DROP TABLE IF EXISTS compliance_scans;

-- 2) One row per scan. No raw text blob here anymore.
CREATE TABLE scans (
    scan_id           VARCHAR(36)  PRIMARY KEY,
    timestamp         DATETIME     NOT NULL,
    image_path        VARCHAR(512),
    overall_status    ENUM('COMPLIANT', 'WARNING', 'NON_COMPLIANT', 'LOW_QUALITY_IMAGE') NOT NULL,
    compliance_score  DECIMAL(5,2),
    is_low_quality    BOOLEAN      DEFAULT FALSE,
    quality_reason    VARCHAR(32),
    real_word_ratio   DECIMAL(4,2),
    glare_ratio       DECIMAL(4,2),
    created_at        TIMESTAMP    DEFAULT CURRENT_TIMESTAMP
);

-- 3) One row per violation found on a scan (0-many per scan).
CREATE TABLE violations (
    violation_id      INT AUTO_INCREMENT PRIMARY KEY,
    scan_id           VARCHAR(36)  NOT NULL,
    rule_reference    VARCHAR(128) NOT NULL,
    severity          ENUM('CRITICAL', 'WARNING', 'MINOR') NOT NULL,
    field_name        VARCHAR(64)  NOT NULL,
    issue_description TEXT         NOT NULL,
    remediation       TEXT         NOT NULL,
    FOREIGN KEY (scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE,
    INDEX idx_violations_scan_id (scan_id)
);

-- 4) One short row per extracted declaration (0-8 per scan: commodity
--    name, net quantity, MRP, mfg/packing date, manufacturer, country of
--    origin, consumer care). This replaces the old raw_ocr_text blob —
--    each field is short, labeled, and queryable on its own instead of
--    being buried in one long paragraph.
CREATE TABLE extracted_fields (
    field_id          INT AUTO_INCREMENT PRIMARY KEY,
    scan_id           VARCHAR(36)  NOT NULL,
    field_name        VARCHAR(64)  NOT NULL,
    field_value       VARCHAR(255) NOT NULL,
    was_detected      BOOLEAN      NOT NULL DEFAULT TRUE,
    FOREIGN KEY (scan_id) REFERENCES scans(scan_id) ON DELETE CASCADE,
    INDEX idx_extracted_fields_scan_id (scan_id)
);

