(function () {
    const STORAGE_KEY = "inventory_last_product_profile_v1";
    const statusEl = document.getElementById("productAutoFillStatus");
    const photoInput = document.getElementById("productPhotoCapture");
    const openPhotoBtn = document.getElementById("openPhotoCaptureBtn");
    const bulkRows = document.getElementById("bulkImeiRows");
    const addRowBtn = document.getElementById("addImeiRowBtn");
    const cloneRows = document.getElementById("cloneImeiRows");
    const addCloneRowBtn = document.getElementById("addCloneImeiRowBtn");
    const cloneSourceProfile = document.getElementById("cloneSourceProfile");
    const cloneProfilePreview = document.getElementById("cloneProfilePreview");
    const singleForm = document.getElementById("singleProductForm");
    const bulkForm = document.getElementById("bulkProductForm");
    const cloneForm = document.getElementById("cloneProductForm");

    if (!singleForm || !bulkForm) {
        return;
    }

    const codeMode = String(bulkForm.dataset.codeMode || singleForm.dataset.codeMode || "imei").toLowerCase();
    const codeLabel = String(bulkForm.dataset.codeLabel || singleForm.dataset.codeLabel || "Code");
    const codeMaxLength = Number(
        bulkForm.dataset.codeMaxlength ||
        singleForm.dataset.codeMaxlength ||
        (codeMode === "imei" ? 15 : 40)
    );

    let rowCounter = 0;
    let isPhotoProcessing = false;

    function setStatus(message) {
        if (statusEl) {
            statusEl.textContent = message;
        }
    }

    function byId(id) {
        return document.getElementById(id);
    }

    function sanitizeCode(value) {
        const raw = String(value || "").trim();
        if (!raw) return "";
        if (codeMode === "imei") {
            return raw.replace(/\D/g, "").slice(0, codeMaxLength);
        }
        return raw.toUpperCase().replace(/[^A-Z0-9._/-]/g, "").slice(0, codeMaxLength);
    }

    function isCodeComplete(value) {
        if (!value) return false;
        if (codeMode === "imei") return value.length >= 15;
        return value.length >= 6;
    }

    function firstNonEmpty(values) {
        for (const value of values) {
            if (value && String(value).trim()) {
                return String(value).trim();
            }
        }
        return "";
    }

    function getProfile(prefix) {
        return {
            brand: byId(prefix + "Brand")?.value?.trim() || "",
            model: byId(prefix + "Model")?.value?.trim() || "",
            category: byId(prefix + "Category")?.value?.trim() || "",
            color: byId(prefix + "Color")?.value?.trim() || "",
            storage: byId(prefix + "Storage")?.value?.trim() || "",
            warranty_type: byId(prefix + "WarrantyType")?.value?.trim() || "",
            note: byId(prefix + "Note")?.value?.trim() || "",
            purchase: byId(prefix + "Purchase")?.value?.trim() || "",
            wholesale: byId(prefix + "Wholesale")?.value?.trim() || "",
            retail: byId(prefix + "Retail")?.value?.trim() || "",
            supplier: byId(prefix + "Supplier")?.value?.trim() || "",
        };
    }

    function applyProfile(prefix, profile) {
        if (!profile) return;
        const mapping = {
            brand: "Brand",
            model: "Model",
            category: "Category",
            color: "Color",
            storage: "Storage",
            warranty_type: "WarrantyType",
            note: "Note",
            purchase: "Purchase",
            wholesale: "Wholesale",
            retail: "Retail",
            supplier: "Supplier",
        };
        Object.entries(mapping).forEach(([key, suffix]) => {
            const input = byId(prefix + suffix);
            if (!input || input.value) return;
            if (profile[key]) {
                input.value = profile[key];
            }
        });
    }

    function saveProfileFromForms() {
        const single = getProfile("single");
        const bulk = getProfile("bulk");
        const merged = {
            brand: firstNonEmpty([single.brand, bulk.brand]),
            model: firstNonEmpty([single.model, bulk.model]),
            category: firstNonEmpty([single.category, bulk.category]),
            color: firstNonEmpty([single.color, bulk.color]),
            storage: firstNonEmpty([single.storage, bulk.storage]),
            warranty_type: firstNonEmpty([single.warranty_type, bulk.warranty_type]),
            note: firstNonEmpty([single.note, bulk.note]),
            purchase: firstNonEmpty([single.purchase, bulk.purchase]),
            wholesale: firstNonEmpty([single.wholesale, bulk.wholesale]),
            retail: firstNonEmpty([single.retail, bulk.retail]),
            supplier: firstNonEmpty([single.supplier, bulk.supplier]),
        };
        localStorage.setItem(STORAGE_KEY, JSON.stringify(merged));
    }

    function restoreProfile() {
        const saved = localStorage.getItem(STORAGE_KEY);
        if (!saved) return;
        try {
            const profile = JSON.parse(saved);
            applyProfile("single", profile);
            applyProfile("bulk", profile);
        } catch (_error) {
            // Ignore bad local storage content.
        }
    }

    function createBulkRow(initialValue = "") {
        rowCounter += 1;
        const imeiId = "bulkImeiInput" + rowCounter;
        const row = document.createElement("div");
        row.className = "inline-field top-gap bulk-imei-row";
        row.innerHTML = `
            <input id="${imeiId}" type="text" name="imei_rows[]" maxlength="${codeMaxLength}" placeholder="${codeLabel}" value="${sanitizeCode(initialValue)}" data-scan-mode="${codeMode}" data-scan-label="${codeLabel}" data-scan-continuous="1">
            <button type="button" class="btn-secondary" data-scan-target="#${imeiId}"><i class="fa-solid fa-barcode"></i>Scan</button>
            <button type="button" class="btn-danger bulk-remove-btn"><i class="fa-solid fa-trash"></i>Remove</button>
        `;
        const input = row.querySelector("input");
        if (input) {
            input.addEventListener("input", function () {
                const normalized = sanitizeCode(input.value);
                input.value = normalized;
                const allInputs = bulkRows.querySelectorAll("input[name='imei_rows[]']");
                const lastInput = allInputs[allInputs.length - 1];
                if (input === lastInput && isCodeComplete(normalized)) {
                    createBulkRow("");
                }
            });
        }
        const removeBtn = row.querySelector(".bulk-remove-btn");
        if (removeBtn) {
            removeBtn.addEventListener("click", function () {
                const currentRows = bulkRows.querySelectorAll(".bulk-imei-row");
                if (currentRows.length <= 1) {
                    const onlyInput = currentRows[0]?.querySelector("input");
                    if (onlyInput) onlyInput.value = "";
                    return;
                }
                row.remove();
            });
        }
        bulkRows.appendChild(row);
    }

    function createCloneRow(initialValue = "") {
        if (!cloneRows) return;
        rowCounter += 1;
        const imeiId = "cloneImeiInput" + rowCounter;
        const row = document.createElement("div");
        row.className = "inline-field top-gap clone-imei-row";
        row.innerHTML = `
            <input id="${imeiId}" type="text" name="imei_rows_clone[]" maxlength="${codeMaxLength}" placeholder="${codeLabel}" value="${sanitizeCode(initialValue)}" data-scan-mode="${codeMode}" data-scan-label="${codeLabel}" data-scan-continuous="1">
            <button type="button" class="btn-secondary" data-scan-target="#${imeiId}"><i class="fa-solid fa-barcode"></i>Scan</button>
            <button type="button" class="btn-danger clone-remove-btn"><i class="fa-solid fa-trash"></i>Remove</button>
        `;
        const input = row.querySelector("input");
        if (input) {
            input.addEventListener("input", function () {
                const normalized = sanitizeCode(input.value);
                input.value = normalized;
                const allInputs = cloneRows.querySelectorAll("input[name='imei_rows_clone[]']");
                const lastInput = allInputs[allInputs.length - 1];
                if (input === lastInput && isCodeComplete(normalized)) {
                    createCloneRow("");
                }
            });
        }
        const removeBtn = row.querySelector(".clone-remove-btn");
        if (removeBtn) {
            removeBtn.addEventListener("click", function () {
                const currentRows = cloneRows.querySelectorAll(".clone-imei-row");
                if (currentRows.length <= 1) {
                    const onlyInput = currentRows[0]?.querySelector("input");
                    if (onlyInput) onlyInput.value = "";
                    return;
                }
                row.remove();
            });
        }
        cloneRows.appendChild(row);
    }

    function findFirstEmptyBulkImeiInput() {
        const inputs = bulkRows.querySelectorAll("input[name='imei_rows[]']");
        for (const input of inputs) {
            if (!input.value.trim()) return input;
        }
        return inputs[0] || null;
    }

    function findFirstEmptyCloneImeiInput() {
        if (!cloneRows) return null;
        const inputs = cloneRows.querySelectorAll("input[name='imei_rows_clone[]']");
        for (const input of inputs) {
            if (!input.value.trim()) return input;
        }
        return inputs[0] || null;
    }

    function setProductFields(fields) {
        const map = {
            brand: "Brand",
            model: "Model",
            category: "Category",
            color: "Color",
            storage: "Storage",
            warranty_type: "WarrantyType",
            note: "Note",
            purchase_price: "Purchase",
            wholesale_price: "Wholesale",
            retail_price: "Retail",
        };

        Object.entries(map).forEach(([key, suffix]) => {
            const value = String(fields[key] || "").trim();
            if (!value) return;
            const single = byId("single" + suffix);
            const bulk = byId("bulk" + suffix);
            if (single && !single.value) single.value = value;
            if (bulk && !bulk.value) bulk.value = value;
        });

        const supplierId = String(fields.supplier_id || "").trim();
        if (supplierId) {
            const singleSupplier = byId("singleSupplier");
            const bulkSupplier = byId("bulkSupplier");
            if (singleSupplier && !singleSupplier.value) singleSupplier.value = supplierId;
            if (bulkSupplier && !bulkSupplier.value) bulkSupplier.value = supplierId;
        }
    }

    function prettyWarrantyLabel(value) {
        const raw = String(value || "").trim().toUpperCase();
        if (raw === "OFFICIAL") return "Official";
        if (raw === "UNOFFICIAL") return "Unofficial";
        return "-";
    }

    async function detectBarcodeFromFile(file) {
        if (!window.BarcodeDetector) return "";
        try {
            const detector = new BarcodeDetector({
                formats: ["code_128", "ean_13", "ean_8", "upc_a", "upc_e", "qr_code"],
            });
            const bitmap = await createImageBitmap(file);
            const results = await detector.detect(bitmap);
            let fallback = "";
            for (const item of results || []) {
                if (!item.rawValue) continue;
                const normalized = sanitizeCode(item.rawValue);
                if (!normalized) continue;
                if (isCodeComplete(normalized)) return normalized;
                if (!fallback) fallback = normalized;
            }
            return fallback;
        } catch (_error) {
            // Ignore detection failure.
        }
        return "";
    }

    async function runOcr(file) {
        if (!window.Tesseract) {
            return "";
        }
        const result = await window.Tesseract.recognize(file, "eng");
        return result?.data?.text || "";
    }

    async function parseTextWithServer(text) {
        const response = await fetch("/api/parse-product-text", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
        });
        if (!response.ok) {
            return { fields: {}, codes: [] };
        }
        const payload = await response.json();
        return {
            fields: payload?.fields || {},
            codes: Array.isArray(payload?.codes) ? payload.codes : [],
            catalogMatch: Boolean(payload?.catalog_match),
            catalogScore: Number(payload?.catalog_score || 0),
            catalogTacMatch: Boolean(payload?.catalog_tac_match),
            safeAutoFill: Boolean(payload?.safe_auto_fill),
        };
    }

    function inferCodeFromText(text) {
        const rawText = String(text || "");
        if (codeMode === "imei") {
            const match = rawText.match(/\d{15}/);
            return match ? sanitizeCode(match[0]) : "";
        }

        const candidates = rawText.toUpperCase().match(/[A-Z0-9][A-Z0-9._/-]{4,39}/g) || [];
        for (const item of candidates) {
            const normalized = sanitizeCode(item);
            if (normalized.length >= 6) {
                return normalized;
            }
        }
        return sanitizeCode(candidates[0] || "");
    }

    async function processPhoto(file) {
        isPhotoProcessing = true;
        setStatus(codeLabel + " photo processing শুরু হয়েছে...");
        try {
            const barcodeImei = await detectBarcodeFromFile(file);
            setStatus("OCR চলছে...");
            const ocrText = await runOcr(file);
            const parsed = await parseTextWithServer(ocrText);
            if (parsed.safeAutoFill) {
                setProductFields(parsed.fields || {});
            }

            const imeiValue = firstNonEmpty([barcodeImei, inferCodeFromText(ocrText)]);
            if (imeiValue) {
                const singleImei = byId("productImeiInput");
                if (singleImei && !singleImei.value) {
                    singleImei.value = sanitizeCode(imeiValue);
                }
                const bulkImei = findFirstEmptyBulkImeiInput();
                if (bulkImei && !bulkImei.value) {
                    bulkImei.value = sanitizeCode(imeiValue);
                    bulkImei.dispatchEvent(new Event("input", { bubbles: true }));
                }
                const cloneImei = findFirstEmptyCloneImeiInput();
                if (cloneImei && !cloneImei.value) {
                    cloneImei.value = sanitizeCode(imeiValue);
                    cloneImei.dispatchEvent(new Event("input", { bubbles: true }));
                }
            }

            const parsedCodes = Array.isArray(parsed.codes) ? parsed.codes : [];
            for (const codeRaw of parsedCodes) {
                const code = sanitizeCode(codeRaw);
                if (!code) continue;
                const bulkImei = findFirstEmptyBulkImeiInput();
                if (bulkImei && !bulkImei.value) {
                    bulkImei.value = code;
                    bulkImei.dispatchEvent(new Event("input", { bubbles: true }));
                }
                const cloneImei = findFirstEmptyCloneImeiInput();
                if (cloneImei && !cloneImei.value) {
                    cloneImei.value = code;
                    cloneImei.dispatchEvent(new Event("input", { bubbles: true }));
                }
            }

            saveProfileFromForms();
            if (parsed.catalogMatch) {
                setStatus(
                    codeLabel +
                        " auto fill completed (Model Catalog matched, score " +
                        parsed.catalogScore +
                        "). Submit এর আগে manual check করুন।"
                );
            } else if (parsed.safeAutoFill) {
                setStatus(codeLabel + " auto fill completed. Submit এর আগে manual check করুন।");
            } else {
                setStatus(
                    "Catalog profile match low confidence. শুধু " +
                        codeLabel +
                        " নেওয়া হয়েছে। Model Profile Search থেকে profile select করুন।"
                );
            }
        } catch (_error) {
            setStatus("Auto fill failed. Manual check করুন।");
        } finally {
            isPhotoProcessing = false;
            if (photoInput) {
                photoInput.value = "";
            }
        }
    }

    function openCameraPicker() {
        if (!photoInput || isPhotoProcessing) return;
        photoInput.click();
    }

    if (openPhotoBtn) {
        openPhotoBtn.addEventListener("click", openCameraPicker);
    }

    if (photoInput) {
        photoInput.addEventListener("change", function () {
            const file = photoInput.files && photoInput.files[0];
            if (file) {
                processPhoto(file);
            }
        });
    }

    if (addRowBtn) {
        addRowBtn.addEventListener("click", function () {
            createBulkRow("");
            const last = bulkRows.querySelector(".bulk-imei-row:last-child input");
            if (last) last.focus();
        });
    }

    if (addCloneRowBtn && cloneRows) {
        addCloneRowBtn.addEventListener("click", function () {
            createCloneRow("");
            const last = cloneRows.querySelector(".clone-imei-row:last-child input");
            if (last) last.focus();
        });
    }

    if (cloneSourceProfile && cloneProfilePreview) {
        cloneSourceProfile.addEventListener("change", function () {
            const selected = cloneSourceProfile.options[cloneSourceProfile.selectedIndex];
            if (!selected || !selected.value) {
                cloneProfilePreview.innerHTML = `
                    <h4 class="section-title"><i class="fa-solid fa-circle-info"></i>Selected Profile</h4>
                    <p class="muted-text">Choose a profile to see details.</p>
                `;
                return;
            }
            cloneProfilePreview.innerHTML = `
                <h4 class="section-title"><i class="fa-solid fa-circle-check"></i>${selected.dataset.brand || "-"} ${selected.dataset.model || ""}</h4>
                <p><strong>Category:</strong> ${selected.dataset.category || "-"}</p>
                <p><strong>Storage/Variant:</strong> ${selected.dataset.storage || "-"}</p>
                <p><strong>Warranty:</strong> ${prettyWarrantyLabel(selected.dataset.warranty || "")}</p>
                <p><strong>Color:</strong> ${selected.dataset.color || "-"}</p>
                <p><strong>Purchase:</strong> ${selected.dataset.purchase || "0"} | <strong>Wholesale:</strong> ${selected.dataset.wholesale || "0"} | <strong>Retail:</strong> ${selected.dataset.retail || "0"}</p>
                <p><strong>Supplier:</strong> ${selected.dataset.supplier || "-"}</p>
            `;
        });
    }

    singleForm.addEventListener("submit", saveProfileFromForms);
    bulkForm.addEventListener("submit", saveProfileFromForms);
    if (cloneForm) {
        cloneForm.addEventListener("submit", saveProfileFromForms);
    }

    restoreProfile();
    createBulkRow("");
    if (cloneRows) {
        createCloneRow("");
    }
    setStatus("Photo Auto Fill button ব্যবহার করুন (" + codeLabel + ")");
})();
