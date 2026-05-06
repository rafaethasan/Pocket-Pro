(function () {
    let overlay = null;
    let activeInput = null;
    let activeMode = "imei";
    let activeLabel = "IMEI";
    let scanBusy = false;
    let lastAppliedValue = "";
    let lastAppliedAt = 0;
    const SCAN_DEDUPE_MS = 1200;

    let html5Scanner = null;
    let barcodeVideo = null;
    let barcodeStream = null;
    let barcodeTrack = null;
    let barcodeDetector = null;
    let barcodeFrame = null;
    let torchEnabled = false;
    let supportedBarcodeFormatsCache = null;

    let tesseractLoader = null;
    let successFxTimer = null;

    function isLikelyMobile() {
        const ua = String(navigator.userAgent || "");
        return /Android|iPhone|iPad|iPod|Mobile|IEMobile|Opera Mini/i.test(ua);
    }

    function shouldPreferBarcodeDetector() {
        if (!window.BarcodeDetector) return false;
        if (activeMode === "imei") return true;
        return isLikelyMobile();
    }

    function getPreferredBarcodeFormats() {
        const formats = ["code_128", "code_39", "ean_13", "ean_8", "upc_a", "upc_e", "qr_code"];
        if (activeMode === "imei") {
            return formats;
        }
        return ["qr_code", "code_128", "code_39", "ean_13", "ean_8", "upc_a", "upc_e"];
    }

    async function getResolvedBarcodeFormats() {
        const preferred = getPreferredBarcodeFormats();
        if (!window.BarcodeDetector || typeof BarcodeDetector.getSupportedFormats !== "function") {
            return preferred;
        }
        if (supportedBarcodeFormatsCache) {
            return preferred.filter((item) => supportedBarcodeFormatsCache.has(item));
        }
        try {
            const supported = await BarcodeDetector.getSupportedFormats();
            supportedBarcodeFormatsCache = new Set((supported || []).map((item) => String(item)));
            const filtered = preferred.filter((item) => supportedBarcodeFormatsCache.has(item));
            return filtered.length ? filtered : preferred;
        } catch (_error) {
            return preferred;
        }
    }

    function getPreferredHtml5Formats() {
        if (!window.Html5QrcodeSupportedFormats) return [];
        const f = window.Html5QrcodeSupportedFormats;
        if (activeMode === "imei") {
            return [f.CODE_128, f.CODE_39, f.EAN_13, f.EAN_8, f.UPC_A, f.UPC_E, f.QR_CODE].filter(Boolean);
        }
        return [f.QR_CODE, f.CODE_128, f.CODE_39, f.EAN_13, f.EAN_8, f.UPC_A, f.UPC_E].filter(Boolean);
    }

    async function getPreferredCameraConfig() {
        if (!window.Html5Qrcode || typeof Html5Qrcode.getCameras !== "function") {
            return { facingMode: { ideal: "environment" } };
        }
        try {
            const cameras = await Html5Qrcode.getCameras();
            if (!Array.isArray(cameras) || cameras.length === 0) {
                return { facingMode: { ideal: "environment" } };
            }
            const preferred = cameras.find((cam) =>
                /back|rear|environment|wide|camera 0/i.test(String(cam.label || ""))
            ) || cameras[0];
            if (preferred && preferred.id) {
                return { deviceId: { exact: preferred.id } };
            }
        } catch (_error) {
            // fallback below
        }
        return { facingMode: { ideal: "environment" } };
    }

    async function applyVideoTrackTune(stream) {
        const track = stream?.getVideoTracks?.()[0];
        if (!track || typeof track.getCapabilities !== "function") return;
        const caps = track.getCapabilities();
        const advanced = [];
        if (Array.isArray(caps.focusMode) && caps.focusMode.includes("continuous")) {
            advanced.push({ focusMode: "continuous" });
        }
        if (caps.exposureMode && Array.isArray(caps.exposureMode) && caps.exposureMode.includes("continuous")) {
            advanced.push({ exposureMode: "continuous" });
        }
        if (advanced.length === 0) return;
        try {
            await track.applyConstraints({ advanced });
        } catch (_error) {
            // ignore capability tuning failures
        }
    }

    async function tuneOverlayScannerVideo() {
        if (!overlay) return;
        const liveVideo =
            overlay.querySelector("#scannerReader video") ||
            overlay.querySelector("#scannerVideo");
        const stream = liveVideo?.srcObject;
        if (!stream) return;
        await applyVideoTrackTune(stream);
        const track = stream.getVideoTracks?.()[0] || null;
        setupTorchControl(track);
    }

    function isValidImei(imei) {
        if (!/^\d{15}$/.test(imei || "")) return false;
        let sum = 0;
        let doubleDigit = false;
        for (let i = imei.length - 1; i >= 0; i -= 1) {
            let digit = Number(imei[i]);
            if (doubleDigit) {
                digit *= 2;
                if (digit > 9) digit -= 9;
            }
            sum += digit;
            doubleDigit = !doubleDigit;
        }
        return sum % 10 === 0;
    }

    function sanitizeGenericCode(rawValue) {
        const text = String(rawValue || "").trim().toUpperCase();
        if (!text) return "";
        return text.replace(/[^A-Z0-9._/-]/g, "").slice(0, 40);
    }

    function extractGenericCodeCandidates(rawValue) {
        const text = String(rawValue || "").toUpperCase();
        const candidates = text.match(/[A-Z0-9][A-Z0-9._/-]{2,39}/g) || [];
        const unique = [];
        const seen = new Set();
        for (const item of candidates) {
            const normalized = sanitizeGenericCode(item);
            if (!normalized || seen.has(normalized)) continue;
            seen.add(normalized);
            unique.push(normalized);
        }
        return unique;
    }

    function extractImeiCandidates(rawValue) {
        const text = String(rawValue || "");
        const candidates = [];

        const normalizedMatches = text.match(/(?:\d[\s-]?){15,18}/g) || [];
        for (const chunk of normalizedMatches) {
            const digits = chunk.replace(/\D/g, "");
            if (digits.length === 15) {
                candidates.push(digits);
            }
            if (digits.length > 15) {
                for (let i = 0; i <= digits.length - 15; i += 1) {
                    candidates.push(digits.slice(i, i + 15));
                }
            }
        }

        const directMatches = text.match(/\d{15}/g) || [];
        candidates.push(...directMatches);

        const unique = [];
        const seen = new Set();
        for (const item of candidates) {
            if (!seen.has(item)) {
                seen.add(item);
                unique.push(item);
            }
        }

        const validFirst = unique.filter(isValidImei);
        if (validFirst.length) return validFirst;
        return unique;
    }

    function normalizeImei(rawValue) {
        const candidates = extractImeiCandidates(rawValue);
        if (candidates.length) {
            return candidates[0];
        }
        const digits = String(rawValue || "").replace(/\D/g, "");
        if (digits.length >= 15) {
            return digits.slice(0, 15);
        }
        return "";
    }

    function normalizeScannedValue(rawValue) {
        if (activeMode === "imei") {
            return normalizeImei(rawValue);
        }

        const genericCandidates = extractGenericCodeCandidates(rawValue);
        if (genericCandidates.length) {
            return genericCandidates[0];
        }
        return sanitizeGenericCode(rawValue);
    }

    function applyScannedValue(rawValue) {
        if (!activeInput) return false;
        const normalized = normalizeScannedValue(rawValue);
        if (!normalized) return false;
        const now = Date.now();
        if (normalized === lastAppliedValue && now - lastAppliedAt < SCAN_DEDUPE_MS) {
            return false;
        }
        lastAppliedValue = normalized;
        lastAppliedAt = now;
        activeInput.value = normalized;
        activeInput.dispatchEvent(new Event("input", { bubbles: true }));
        activeInput.dispatchEvent(new Event("change", { bubbles: true }));
        activeInput.dispatchEvent(
            new CustomEvent("scan:applied", {
                bubbles: true,
                detail: { value: normalized, mode: activeMode, label: activeLabel },
            })
        );
        return true;
    }

    function isContinuousScanInputTarget(input) {
        if (!input) return false;
        if (String(input.dataset.scanContinuous || "") === "1") return true;
        const name = String(input.name || "");
        return name === "imei_rows[]" || name === "imei_rows_clone[]";
    }

    function listSameNameInputs(scope, name) {
        if (!scope || !name) return [];
        return Array.from(scope.querySelectorAll("input")).filter((item) => String(item.name || "") === name);
    }

    async function moveToNextContinuousInput() {
        const current = activeInput;
        if (!current) return false;
        const scope = current.closest("form") || document;
        const targetName = String(current.name || "");
        if (!targetName) return false;

        for (let attempt = 0; attempt < 6; attempt += 1) {
            const inputs = listSameNameInputs(scope, targetName);
            const next = inputs.find((item) => !String(item.value || "").trim());
            if (next) {
                activeInput = next;
                next.focus({ preventScroll: true });
                return true;
            }
            await new Promise((resolve) => setTimeout(resolve, 70));
        }
        return false;
    }

    function hapticFeedback() {
        try {
            if (navigator.vibrate) {
                navigator.vibrate(18);
            }
        } catch (_error) {
            // ignore
        }
    }

    async function handleDetectedRawValue(rawValue) {
        if (scanBusy) return false;
        scanBusy = true;
        setScanVisualState("processing");
        try {
            const applied = applyScannedValue(rawValue);
            if (!applied) {
                setScanVisualState("searching");
                return false;
            }

            hapticFeedback();
            showScanSuccessEffect();

            if (isContinuousScanInputTarget(activeInput)) {
                await moveToNextContinuousInput();
                setScanNote(`${activeLabel} added. Keep camera on barcode for next one.`);
                return true;
            }

            await new Promise((resolve) => setTimeout(resolve, 260));
            await stopScanner();
            return true;
        } finally {
            scanBusy = false;
            const successEl = overlay?.querySelector?.("#scannerSuccessFx");
            if (overlay && !(successEl && successEl.classList.contains("show"))) {
                setScanVisualState("searching");
            }
        }
    }

    function setScanNote(message) {
        if (!overlay) return;
        const note = overlay.querySelector(".scanner-note");
        if (!note) return;
        note.textContent = message;
    }

    function setScanVisualState(state) {
        if (!overlay) return;
        const stage = overlay.querySelector("#scannerStage");
        if (!stage) return;
        stage.classList.remove("scan-state-searching", "scan-state-processing", "scan-state-success");
        if (state) {
            stage.classList.add("scan-state-" + state);
        }
    }

    function showScanSuccessEffect() {
        if (!overlay) return;
        const successEl = overlay.querySelector("#scannerSuccessFx");
        if (!successEl) return;
        if (successFxTimer) {
            clearTimeout(successFxTimer);
            successFxTimer = null;
        }
        successEl.classList.remove("show");
        // Restart CSS transition/animation for repeated scans.
        void successEl.offsetWidth;
        successEl.classList.add("show");
        setScanVisualState("success");
        successFxTimer = window.setTimeout(function () {
            if (!overlay) return;
            successEl.classList.remove("show");
            setScanVisualState("searching");
        }, 680);
    }

    async function stopHtml5Scanner() {
        if (!html5Scanner) return;
        try {
            await html5Scanner.stop();
        } catch (_error) {
            // ignore
        }
        try {
            await html5Scanner.clear();
        } catch (_error) {
            // ignore
        }
        html5Scanner = null;
    }

    function stopBarcodeDetectorScanner() {
        if (barcodeFrame) {
            cancelAnimationFrame(barcodeFrame);
            barcodeFrame = null;
        }
        if (barcodeStream) {
            for (const track of barcodeStream.getTracks()) {
                track.stop();
            }
            barcodeStream = null;
        }
        barcodeTrack = null;
        torchEnabled = false;
        barcodeVideo = null;
        barcodeDetector = null;
        const torchBtn = overlay?.querySelector?.("#scannerTorchBtn");
        if (torchBtn) {
            torchBtn.hidden = true;
            torchBtn.onclick = null;
        }
    }

    async function stopScanner() {
        if (successFxTimer) {
            clearTimeout(successFxTimer);
            successFxTimer = null;
        }
        await stopHtml5Scanner();
        stopBarcodeDetectorScanner();
        if (overlay) {
            overlay.remove();
            overlay = null;
        }
        activeInput = null;
        activeMode = "imei";
        activeLabel = "IMEI";
        scanBusy = false;
        lastAppliedValue = "";
        lastAppliedAt = 0;
    }

    function createOverlay() {
        overlay = document.createElement("div");
        overlay.className = "scanner-overlay";
        overlay.innerHTML = `
            <div class="scanner-box">
                <div class="scanner-head">
                    <strong>${activeLabel} Scan</strong>
                    <button type="button" class="btn-danger" id="scannerCloseBtn"><i class="fa-solid fa-xmark"></i>Close</button>
                </div>
                <div id="scannerStage" class="scanner-stage scan-state-searching">
                    <div id="scannerReader" class="scanner-reader"></div>
                    <video id="scannerVideo" class="scanner-video" autoplay playsinline muted></video>
                    <div class="scanner-fx" aria-hidden="true">
                        <span class="scanner-target-box"></span>
                        <span class="scanner-scan-line"></span>
                    </div>
                    <div class="scanner-success" id="scannerSuccessFx" aria-hidden="true">
                        <span class="scanner-success-icon"><i class="fa-solid fa-check"></i></span>
                        <strong>Scan Complete</strong>
                    </div>
                </div>
                <div class="scanner-actions">
                    <button type="button" class="btn-secondary" id="scannerCaptureBtn"><i class="fa-solid fa-camera"></i>Capture</button>
                    <button type="button" class="btn-secondary" id="scannerTorchBtn" hidden><i class="fa-solid fa-bolt"></i>Torch Off</button>
                    <button type="button" class="btn-secondary" id="scannerPhotoBtn"><i class="fa-solid fa-image"></i>Scan From Photo</button>
                </div>
                <input id="scannerPhotoInput" type="file" accept="image/*" capture="environment" hidden>
                <div class="scanner-note">
                    Camera দিয়ে barcode scan করুন। কাজ না করলে Scan From Photo ব্যবহার করুন (${activeLabel})।
                </div>
            </div>
        `;
        document.body.appendChild(overlay);

        overlay.querySelector("#scannerCloseBtn").addEventListener("click", function () {
            stopScanner();
        });
    }

    function getLiveVideoElement() {
        if (!overlay) return null;
        const nativeVideo = overlay.querySelector("#scannerVideo");
        if (nativeVideo && nativeVideo.srcObject) return nativeVideo;
        const html5Video = overlay.querySelector("#scannerReader video");
        if (html5Video && html5Video.srcObject) return html5Video;
        return nativeVideo || html5Video;
    }

    function captureCurrentFrameCanvas() {
        const video = getLiveVideoElement();
        if (!video) return null;
        const vw = Number(video.videoWidth || 0);
        const vh = Number(video.videoHeight || 0);
        if (vw < 32 || vh < 32) return null;
        const canvas = document.createElement("canvas");
        canvas.width = vw;
        canvas.height = vh;
        const ctx = canvas.getContext("2d");
        if (!ctx) return null;
        ctx.drawImage(video, 0, 0, vw, vh);
        return canvas;
    }

    async function scanFromCanvasWithBarcodeDetector(canvas) {
        if (!canvas || !window.BarcodeDetector) return false;
        try {
            const formats = await getResolvedBarcodeFormats();
            const detector = new BarcodeDetector({ formats });
            const codes = await detector.detect(canvas);
            for (const code of codes || []) {
                if (!code?.rawValue) continue;
                if (await handleDetectedRawValue(code.rawValue)) {
                    return true;
                }
            }
        } catch (_error) {
            // ignore
        }
        return false;
    }

    async function scanFromCanvasWithOcr(canvas) {
        if (!canvas || activeMode !== "imei") return false;
        const loaded = await ensureTesseractLoaded();
        if (!loaded || !window.Tesseract) return false;
        try {
            const result = await window.Tesseract.recognize(canvas, "eng");
            const text = result?.data?.text || "";
            return await handleDetectedRawValue(text);
        } catch (_error) {
            return false;
        }
    }

    async function captureAndDecodeFrame() {
        setScanVisualState("processing");
        const canvas = captureCurrentFrameCanvas();
        if (!canvas) {
            setScanNote("Camera frame ready নয়। 1 সেকেন্ড অপেক্ষা করে আবার try করুন।");
            setScanVisualState("searching");
            return false;
        }
        setScanNote("Capturing frame...");
        const barcodeOk = await scanFromCanvasWithBarcodeDetector(canvas);
        if (barcodeOk) return true;
        setScanNote("Barcode detect হয়নি, OCR দিয়ে চেষ্টা চলছে...");
        const ocrOk = await scanFromCanvasWithOcr(canvas);
        if (ocrOk) return true;
        setScanNote("Detect হয়নি। একটু কাছে এনে আবার scan/capture দিন।");
        setScanVisualState("searching");
        return false;
    }

    function setTorchButtonState() {
        if (!overlay) return;
        const torchBtn = overlay.querySelector("#scannerTorchBtn");
        if (!torchBtn) return;
        torchBtn.innerHTML = torchEnabled
            ? '<i class="fa-solid fa-bolt"></i>Torch On'
            : '<i class="fa-solid fa-bolt"></i>Torch Off';
    }

    function setupTorchControl(track) {
        if (!overlay) return;
        const torchBtn = overlay.querySelector("#scannerTorchBtn");
        if (!torchBtn) return;
        torchBtn.hidden = true;
        torchBtn.onclick = null;
        torchEnabled = false;

        if (!track || typeof track.getCapabilities !== "function") return;
        let capabilities = null;
        try {
            capabilities = track.getCapabilities();
        } catch (_error) {
            capabilities = null;
        }
        if (!capabilities || !capabilities.torch) return;

        torchBtn.hidden = false;
        setTorchButtonState();
        torchBtn.onclick = async function () {
            const desired = !torchEnabled;
            try {
                await track.applyConstraints({ advanced: [{ torch: desired }] });
                torchEnabled = desired;
                setTorchButtonState();
            } catch (_error) {
                // ignore toggle failure
            }
        };
    }

    async function ensureTesseractLoaded() {
        if (window.SOFTX_OFFLINE_MODE) return false;
        if (window.Tesseract) return true;
        if (tesseractLoader) return tesseractLoader;

        tesseractLoader = new Promise((resolve) => {
            const existing = document.querySelector("script[data-scan-tesseract='1']");
            if (existing) {
                existing.addEventListener("load", function () {
                    resolve(Boolean(window.Tesseract));
                });
                existing.addEventListener("error", function () {
                    resolve(false);
                });
                return;
            }

            const script = document.createElement("script");
            script.src = "https://cdn.jsdelivr.net/npm/tesseract.js@5/dist/tesseract.min.js";
            script.async = true;
            script.defer = true;
            script.setAttribute("data-scan-tesseract", "1");
            script.onload = function () {
                resolve(Boolean(window.Tesseract));
            };
            script.onerror = function () {
                resolve(false);
            };
            document.head.appendChild(script);
        });

        return tesseractLoader;
    }

    async function scanFromImageWithHtml5(file) {
        if (!window.Html5Qrcode) return false;
        const readerId = "scannerReader";
        let tempScanner = null;
        try {
            tempScanner = new Html5Qrcode(readerId);
            const decoded = await tempScanner.scanFile(file, true);
            return applyScannedValue(decoded);
        } catch (_error) {
            return false;
        } finally {
            if (tempScanner) {
                try {
                    await tempScanner.clear();
                } catch (_error) {
                    // ignore
                }
            }
        }
    }

    async function scanFromImageWithBarcodeDetector(file) {
        if (!window.BarcodeDetector) return false;
        try {
            const formats = await getResolvedBarcodeFormats();
            const detector = new BarcodeDetector({
                formats,
            });
            const bitmap = await createImageBitmap(file);
            const codes = await detector.detect(bitmap);
            for (const code of codes || []) {
                if (applyScannedValue(code.rawValue)) {
                    return true;
                }
            }
        } catch (_error) {
            // ignore
        }
        return false;
    }

    async function scanFromImageWithOcr(file) {
        const loaded = await ensureTesseractLoaded();
        if (!loaded || !window.Tesseract) return false;

        try {
            const result = await window.Tesseract.recognize(file, "eng");
            const text = result?.data?.text || "";
            return applyScannedValue(text);
        } catch (_error) {
            return false;
        }
    }

    async function scanFromImageFile(file) {
        if (!file) return false;

        setScanVisualState("processing");
        setScanNote("Image processing চলছে...");

        const html5Ok = await scanFromImageWithHtml5(file);
        if (html5Ok) return true;

        const detectorOk = await scanFromImageWithBarcodeDetector(file);
        if (detectorOk) return true;

        setScanNote("Barcode পাওয়া যায়নি, OCR দিয়ে " + activeLabel + " খোঁজা হচ্ছে...");
        const ocrOk = await scanFromImageWithOcr(file);
        if (ocrOk) return true;

        setScanVisualState("searching");
        return false;
    }

    async function startHtml5QrcodeScanner() {
        if (!window.Html5Qrcode) return false;

        const readerEl = overlay.querySelector("#scannerReader");
        const videoEl = overlay.querySelector("#scannerVideo");
        if (videoEl) {
            videoEl.style.display = "none";
        }
        if (!readerEl) return false;

        html5Scanner = new Html5Qrcode("scannerReader");
        const viewportWidth = Math.max(280, Math.min(window.innerWidth || 360, 720));
        const qrBoxWidth = Math.round(Math.min(420, viewportWidth * 0.88));
        const qrBoxHeight = activeMode === "imei" ? 132 : Math.round(qrBoxWidth * 0.68);
        const supportedFormats = getPreferredHtml5Formats();
        const config = {
            fps: activeMode === "imei" ? 22 : 14,
            qrbox: { width: qrBoxWidth, height: qrBoxHeight },
            aspectRatio: (window.innerWidth || 390) > (window.innerHeight || 740) ? 1.777 : 1.333,
            disableFlip: true,
            rememberLastUsedCamera: true,
            experimentalFeatures: { useBarCodeDetectorIfSupported: true },
        };
        if (supportedFormats.length) {
            config.formatsToSupport = supportedFormats;
        }

        const onSuccess = async function (decodedText) {
            await handleDetectedRawValue(decodedText);
        };

        const onFailure = function () {
            // keep scanning
        };

        const preferredCamera = await getPreferredCameraConfig();
        try {
            await html5Scanner.start(preferredCamera, config, onSuccess, onFailure);
            await tuneOverlayScannerVideo();
            return true;
        } catch (_error) {
            try {
                await html5Scanner.start({ facingMode: { ideal: "environment" } }, config, onSuccess, onFailure);
                await tuneOverlayScannerVideo();
                return true;
            } catch (_errorAgain) {
                try {
                    await html5Scanner.start({ facingMode: "user" }, config, onSuccess, onFailure);
                    await tuneOverlayScannerVideo();
                    return true;
                } catch (_finalError) {
                    await stopHtml5Scanner();
                    return false;
                }
            }
        }
    }

    async function barcodeLoop() {
        if (!barcodeDetector || !barcodeVideo) return;
        try {
            const barcodes = await barcodeDetector.detect(barcodeVideo);
            if (barcodes && barcodes.length > 0) {
                for (const code of barcodes) {
                    if (!code || !code.rawValue) continue;
                    const handled = await handleDetectedRawValue(code.rawValue);
                    if (handled && !overlay) {
                        return;
                    }
                }
            }
        } catch (_error) {
            // continue loop
        }
        barcodeFrame = requestAnimationFrame(barcodeLoop);
    }

    async function startBarcodeDetectorScanner() {
        if (!window.BarcodeDetector) return false;
        if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) return false;

        const videoEl = overlay.querySelector("#scannerVideo");
        const readerEl = overlay.querySelector("#scannerReader");
        if (readerEl) {
            readerEl.style.display = "none";
        }
        if (!videoEl) return false;

        barcodeVideo = videoEl;
        barcodeVideo.style.display = "block";

        barcodeDetector = new BarcodeDetector({
            formats: await getResolvedBarcodeFormats(),
        });

        const preferredCamera = await getPreferredCameraConfig();
        const videoConstraints = {
            width: { ideal: 1280, min: 640 },
            height: { ideal: 720, min: 360 },
            frameRate: { ideal: 30, max: 60 },
            ...preferredCamera,
        };
        if (!videoConstraints.facingMode && !videoConstraints.deviceId) {
            videoConstraints.facingMode = { ideal: "environment" };
        }

        try {
            barcodeStream = await navigator.mediaDevices.getUserMedia({
                video: videoConstraints,
                audio: false,
            });
        } catch (_error) {
            try {
                barcodeStream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: { ideal: "environment" },
                        width: { ideal: 1280 },
                        height: { ideal: 720 },
                    },
                    audio: false,
                });
            } catch (_fallbackError) {
                return false;
            }
        }

        await applyVideoTrackTune(barcodeStream);
        barcodeTrack = barcodeStream.getVideoTracks?.()[0] || null;
        setupTorchControl(barcodeTrack);
        barcodeVideo.srcObject = barcodeStream;
        await barcodeVideo.play();
        barcodeFrame = requestAnimationFrame(barcodeLoop);
        return true;
    }

    async function restartLiveScan() {
        setScanVisualState("processing");
        setScanNote("Starting fast live scan...");
        if (shouldPreferBarcodeDetector()) {
            const barcodeStarted = await startBarcodeDetectorScanner();
            if (barcodeStarted) {
                setScanVisualState("searching");
                setScanNote("Fast live scan ready: keep barcode steady, about 15-25 cm distance.");
                return;
            }
        }

        const html5Started = await startHtml5QrcodeScanner();
        if (html5Started) {
            setScanVisualState("searching");
            setScanNote("Live scan active: barcode frame-এর মাঝখানে ধরুন, 20-30 সেমি দূরত্ব রাখুন।");
            return;
        }

        if (!shouldPreferBarcodeDetector()) {
            const barcodeStarted = await startBarcodeDetectorScanner();
            if (barcodeStarted) {
                setScanVisualState("searching");
                setScanNote("Live scan active: barcode frame-এর মাঝখানে ধরুন, 20-30 সেমি দূরত্ব রাখুন।");
                return;
            }
        }

        setScanVisualState("searching");
        setScanNote("এই ব্রাউজারে live scan support কম। Photo Scan বা manual ব্যবহার করুন।");
    }

    async function startScanner(input) {
        if (overlay) {
            await stopScanner();
        }
        activeInput = input;
        activeMode = String(input.dataset.scanMode || "imei").toLowerCase();
        activeLabel = String(input.dataset.scanLabel || (activeMode === "imei" ? "IMEI" : "Code"));
        createOverlay();
        if (isContinuousScanInputTarget(activeInput)) {
            setScanNote("Fast continuous mode: scan one by one, next line auto selected.");
        }

        const photoBtn = overlay.querySelector("#scannerPhotoBtn");
        const photoInput = overlay.querySelector("#scannerPhotoInput");
        const captureBtn = overlay.querySelector("#scannerCaptureBtn");
        if (captureBtn) {
            captureBtn.addEventListener("click", async function () {
                await captureAndDecodeFrame();
            });
        }
        const tapVideo = () => {
            captureAndDecodeFrame();
        };
        const nativeVideo = overlay.querySelector("#scannerVideo");
        if (nativeVideo) {
            nativeVideo.addEventListener("click", tapVideo);
        }
        const readerEl = overlay.querySelector("#scannerReader");
        if (readerEl) {
            readerEl.addEventListener("click", function (event) {
                if (event.target && event.target.closest && event.target.closest("video")) {
                    tapVideo();
                }
            });
        }
        if (photoBtn && photoInput) {
            photoBtn.addEventListener("click", function () {
                photoInput.click();
            });
            photoInput.addEventListener("change", async function () {
                const file = photoInput.files && photoInput.files[0];
                photoInput.value = "";
                if (!file) return;

                // Pause live scanner first. Otherwise scanFile can fail in some browsers.
                await stopHtml5Scanner();
                stopBarcodeDetectorScanner();

                const ok = await scanFromImageFile(file);
                if (ok) {
                    await stopScanner();
                    return;
                }

                alert(activeLabel + " detect করা যায়নি। পরিষ্কার ছবি দিন (barcode/number close shot) বা manual লিখুন।");
                await restartLiveScan();
            });
        }

        await restartLiveScan();
    }

    document.addEventListener("click", async function (event) {
        const trigger = event.target.closest("[data-scan-target]");
        if (!trigger) return;
        event.preventDefault();

        const selector = trigger.getAttribute("data-scan-target");
        const input = document.querySelector(selector);
        if (!input) return;

        try {
            await startScanner(input);
        } catch (_error) {
            await stopScanner();
            alert("Camera scan শুরু করা যায়নি। " + activeLabel + " manual লিখুন।");
        }
    });

    window.addEventListener("beforeunload", function () {
        stopScanner();
    });
})();
