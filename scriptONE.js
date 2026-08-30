/* ============================================================
   LM·CS frontend logic
   Three jobs: (1) let the user select/reset a label image,
   (2) show a waiting state while the backend scans it,
   (3) render whatever compliance JSON comes back.

   ---------------------------------------------------------
   ATTACHMENT POINT #1 — where this talks to Flask.
   Change SCAN_ENDPOINT if your teammate's route is named
   differently. The request is a multipart POST with the
   image under the field name "label_image".
   ---------------------------------------------------------
*/

const CONFIG = {
  SCAN_ENDPOINT: "/api/scan",   // Flask route: accepts the image, returns the compliance JSON
  FILE_FIELD_NAME: "label_image",
  DEMO_MODE_DEFAULT: false      // flip the "Sample data mode" checkbox in the UI instead of editing this
};

const PIPELINE_STEPS = [
  "Uploading image",
  "Preprocessing & dewarping",
  "Running OCR extraction",
  "Parsing key fields",
  "Validating against LMPC rules"
];

// Sample response, matching the exact JSON shape the backend is expected
// to return. Used only when "Sample data mode" is checked, so the UI can
// be built/demoed before the real /api/scan route exists.
const MOCK_RESPONSE = {
  audit_metadata: {
    scan_id: "b6b6b1a0-demo-4e1a-9c2e-000000000000",
    timestamp: new Date().toISOString(),
    overall_status: "WARNING",
    compliance_score_percentage: 75.0
  },
  extracted_entities: {
    commodity_name: "Refined Sunflower Oil",
    net_quantity: { raw_text: "500 g", value: 500, unit: "g", is_valid_metric_unit: true },
    mrp: { raw_text: "MRP Rs. 150.00 (inclusive of all taxes)", amount: 150.00, formatted_correctly: true, inclusive_of_all_taxes: true },
    date_declaration: { raw_text: "MFG 05/2026", type: "MANUFACTURE", month: 5, year: 2026, is_present: true },
    manufacturer_details: { raw_text: "MFD BY XYZ Foods Ltd, Industrial Area, Pune, Maharashtra 411001", is_address_complete: true },
    country_of_origin: { raw_text: "Made in India", country: "INDIA", is_declared: true },
    consumer_care: { raw_text: "1800-123-4567", is_present: true }
  },
  violations_found: [
    {
      rule_reference: "Rule 6(1)(f)",
      severity: "WARNING",
      field: "Unit Sale Price",
      issue: "Unit sale price (price per unit weight/volume) not detected.",
      remediation: "Declare unit sale price where applicable (e.g., Rs. per 100g or per kg)."
    }
  ]
};

// ---------- element refs ----------
const dropzone       = document.getElementById("dropzone");
const dropzoneEmpty  = document.getElementById("dropzoneEmpty");
const previewImage   = document.getElementById("previewImage");
const scanSweep      = document.getElementById("scanSweep");
const fileInput      = document.getElementById("fileInput");
const fileStatus     = document.getElementById("fileStatus");
const selectBtn      = document.getElementById("selectBtn");
const cameraBtn      = document.getElementById("cameraBtn");
const captureBtn     = document.getElementById("captureBtn");
const cameraContainer = document.getElementById("cameraContainer");
const cameraPreview  = document.getElementById("cameraPreview");
const cameraCanvas   = document.getElementById("cameraCanvas");
const scanBtn        = document.getElementById("scanBtn");
const resetBtn       = document.getElementById("resetBtn");
const retryBtn       = document.getElementById("retryBtn");
const demoModeToggle = document.getElementById("demoModeToggle");

const stateIdle    = document.getElementById("stateIdle");
const stateLoading = document.getElementById("stateLoading");
const stateError   = document.getElementById("stateError");
const stateResult  = document.getElementById("stateResult");

const timerValue   = document.getElementById("timerValue");
const stepList     = document.getElementById("stepList");
const errorText    = document.getElementById("errorText");
const statusStamp  = document.getElementById("statusStamp");
const scoreValue   = document.getElementById("scoreValue");
const entitiesList = document.getElementById("entitiesList");
const violationsList = document.getElementById("violationsList");

let selectedFile = null;
let loadingInterval = null;
let stepTimeout = null;
let cameraStream = null;

demoModeToggle.checked = CONFIG.DEMO_MODE_DEFAULT;

// ---------- SELECT ----------

selectBtn.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("click", () => fileInput.click());
dropzone.addEventListener("keydown", (e) => {
  if (e.key === "Enter" || e.key === " ") { e.preventDefault(); fileInput.click(); }
});

fileInput.addEventListener("change", (e) => {
  if (e.target.files && e.target.files[0]) handleFileSelected(e.target.files[0]);
});

["dragover", "dragenter"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.add("is-drag"); })
);
["dragleave", "drop"].forEach((evt) =>
  dropzone.addEventListener(evt, (e) => { e.preventDefault(); dropzone.classList.remove("is-drag"); })
);
dropzone.addEventListener("drop", (e) => {
  const file = e.dataTransfer.files && e.dataTransfer.files[0];
  if (file) handleFileSelected(file);
});

function handleFileSelected(file) {
  if (!file.type.startsWith("image/")) {
    fileStatus.textContent = "Please select an image file (jpg, png, etc).";
    return;
  }
  selectedFile = file;

  const url = URL.createObjectURL(file);
  previewImage.src = url;
  previewImage.hidden = false;
  dropzoneEmpty.hidden = true;

  fileStatus.textContent = `Selected: ${file.name}`;
  scanBtn.disabled = false;

  showState(stateIdle);
}

// ---------- RESET ----------

resetBtn.addEventListener("click", resetAll);
cameraBtn.addEventListener("click", toggleCamera);
captureBtn.addEventListener("click", captureCameraFrame);

function resetAll() {
  selectedFile = null;
  fileInput.value = "";

  if (previewImage.src) URL.revokeObjectURL(previewImage.src);
  previewImage.src = "";
  previewImage.hidden = true;
  dropzoneEmpty.hidden = false;
  scanSweep.hidden = true;
  dropzone.classList.remove("is-drag");

  stopCamera();

  fileStatus.textContent = "No file selected";
  scanBtn.disabled = true;

  clearInterval(loadingInterval);
  clearTimeout(stepTimeout);

  showState(stateIdle);
}

async function toggleCamera() {
  if (cameraStream) {
    stopCamera();
    return;
  }

  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    fileStatus.textContent = "Camera access is not supported in this browser.";
    return;
  }

  try {
    cameraStream = await navigator.mediaDevices.getUserMedia({
      video: {
        facingMode: "environment",
        width: { ideal: 1920 },
        height: { ideal: 1080 }
      },
      audio: false
    });

    cameraPreview.srcObject = cameraStream;
    cameraContainer.hidden = false;
    captureBtn.hidden = false;
    cameraBtn.textContent = "Stop Camera";
    fileStatus.textContent = "Camera ready — capture the product label.";
  } catch (error) {
    fileStatus.textContent = "Camera permission denied or unavailable.";
    console.error("Camera error:", error);
  }
}

function stopCamera() {
  if (cameraStream) {
    cameraStream.getTracks().forEach((track) => track.stop());
    cameraStream = null;
  }

  if (cameraPreview) cameraPreview.srcObject = null;
  cameraContainer.hidden = true;
  captureBtn.hidden = true;
  cameraBtn.textContent = "Use Camera";
}

function captureCameraFrame() {
  if (!cameraStream || !cameraPreview.videoWidth) {
    fileStatus.textContent = "Camera is not active.";
    return;
  }

  const width = cameraPreview.videoWidth;
  const height = cameraPreview.videoHeight;
  cameraCanvas.width = width;
  cameraCanvas.height = height;

  const context = cameraCanvas.getContext("2d");
  context.drawImage(cameraPreview, 0, 0, width, height);

  cameraCanvas.toBlob((blob) => {
    if (!blob) {
      fileStatus.textContent = "Image capture failed. Please try again.";
      return;
    }

    const capturedFile = new File([blob], `captured_label_${Date.now()}.png`, { type: "image/png" });
    handleFileSelected(capturedFile);
    stopCamera();
  }, "image/png");
}

// ---------- WAITING / COUNTDOWN + SCAN ----------

scanBtn.addEventListener("click", runScan);
retryBtn.addEventListener("click", runScan);

function runScan() {
  if (!selectedFile) return;

  showState(stateLoading);
  scanSweep.hidden = false;
  scanBtn.disabled = true;

  startCountdown();
  startStepper();

  const usesDemoData = demoModeToggle.checked;

  const backendCall = usesDemoData
    ? mockScanRequest(selectedFile)
    : realScanRequest(selectedFile);

  backendCall
    .then((data) => {
      finishLoading();
      renderReport(data);
      showState(stateResult);
    })
    .catch((err) => {
      finishLoading();
      errorText.textContent =
        "Couldn't reach the compliance engine. Check that the Flask server is running and " +
        `that ${CONFIG.SCAN_ENDPOINT} exists. (${err.message})`;
      showState(stateError);
    })
    .finally(() => {
      scanSweep.hidden = true;
      scanBtn.disabled = false;
    });
}

// ---------------------------------------------------------
// ATTACHMENT POINT #2 — the actual network call to Flask.
// FormData key must match request.files["label_image"]
// on the backend (see app_example.py).
// ---------------------------------------------------------
function realScanRequest(file) {
  const formData = new FormData();
  formData.append(CONFIG.FILE_FIELD_NAME, file);

  return fetch(CONFIG.SCAN_ENDPOINT, {
    method: "POST",
    body: formData
  }).then((res) => {
    if (!res.ok) throw new Error(`Server responded ${res.status}`);
    return res.json();
  });
}

function mockScanRequest() {
  return new Promise((resolve) => {
    setTimeout(() => resolve(MOCK_RESPONSE), 2600);
  });
}

function startCountdown() {
  const startedAt = Date.now();
  timerValue.textContent = "0.0";
  loadingInterval = setInterval(() => {
    const elapsed = ((Date.now() - startedAt) / 1000).toFixed(1);
    timerValue.textContent = elapsed;
  }, 100);
}

function startStepper() {
  stepList.innerHTML = "";
  PIPELINE_STEPS.forEach((label) => {
    const li = document.createElement("li");
    li.className = "step-pending";
    li.innerHTML = `<span class="step-marker"></span><span>${label}</span>`;
    stepList.appendChild(li);
  });

  let i = 0;
  const advance = () => {
    const items = stepList.querySelectorAll("li");
    if (i > 0) items[i - 1].className = "step-done";
    if (i < items.length) {
      items[i].className = "step-active";
      i++;
      // last step waits for the real response rather than auto-advancing,
      // so it doesn't lie about being finished before data arrives
      if (i < items.length) stepTimeout = setTimeout(advance, 700);
    }
  };
  advance();
}

function finishLoading() {
  clearInterval(loadingInterval);
  clearTimeout(stepTimeout);
  const items = stepList.querySelectorAll("li");
  items.forEach((li) => (li.className = "step-done"));
}

// ---------- RENDER RESULT ----------

function renderReport(data) {
  const meta = data.audit_metadata || {};
  const entities = data.extracted_entities || {};
  const violations = data.violations_found || [];

  statusStamp.textContent = (meta.overall_status || "UNKNOWN").replace("_", "-");
  statusStamp.className = "stamp";
  if (meta.overall_status === "WARNING") statusStamp.classList.add("is-warning");
  if (meta.overall_status === "NON_COMPLIANT") statusStamp.classList.add("is-critical");
  if (meta.overall_status === "LOW_QUALITY_IMAGE") statusStamp.classList.add("is-low-quality");

  scoreValue.textContent =
    meta.compliance_score_percentage != null ? `${meta.compliance_score_percentage}%` : "—";

  entitiesList.innerHTML = "";
  const rows = [
    ["Commodity", entities.commodity_name],
    ["Net quantity", entities.net_quantity && entities.net_quantity.raw_text],
    ["MRP", entities.mrp && entities.mrp.raw_text],
    ["Unit sale price", entities.unit_sale_price && entities.unit_sale_price.raw_text],
    ["Mfg / packing date", entities.date_declaration && entities.date_declaration.raw_text],
    ["Manufacturer", entities.manufacturer_details && entities.manufacturer_details.raw_text],
    ["Country of origin", entities.country_of_origin && entities.country_of_origin.raw_text],
    ["Consumer care", entities.consumer_care && entities.consumer_care.raw_text]
  ];
  rows.forEach(([label, value]) => {
    if (!value) return;
    const row = document.createElement("div");
    row.className = "entity-row";
    row.innerHTML = `<dt>${label}</dt><dd>${escapeHtml(String(value))}</dd>`;
    entitiesList.appendChild(row);
  });

  violationsList.innerHTML = "";
  if (violations.length === 0) {
    violationsList.innerHTML = `<p class="no-violations">No violations detected.</p>`;
  } else {
    violations.forEach((v) => {
      const card = document.createElement("div");
      const isWarning = (v.severity || "").toUpperCase() === "WARNING";
      card.className = "violation-card" + (isWarning ? " severity-warning" : "");
      card.innerHTML = `
        <div class="violation-head">
          <span class="violation-rule">${escapeHtml(v.rule_reference || "")}</span>
          <span class="severity-badge">${escapeHtml(v.severity || "")}</span>
        </div>
        <p class="violation-issue">${escapeHtml(v.issue || "")}</p>
        <p class="violation-fix">${escapeHtml(v.remediation || "")}</p>
      `;
      violationsList.appendChild(card);
    });
  }
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

// ---------- state switching ----------

function showState(target) {
  [stateIdle, stateLoading, stateError, stateResult].forEach((el) => {
    el.hidden = el !== target;
  });
}
