(() => {
  "use strict";
  document.documentElement.classList.add("js-ready");
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content || "";
  const csrfHeaders = (extra = {}) => ({...extra, "X-CSRF-Token": csrfToken});

  const activateDetail = (row) => {
    const workspace = row.closest(".workspace-layout");
    if (!workspace) return;
    workspace.querySelectorAll(".selectable-row").forEach((candidate) => {
      const selected = candidate === row;
      candidate.classList.toggle("is-selected", selected);
      candidate.setAttribute("aria-selected", String(selected));
    });
    workspace.querySelectorAll(".detail-view").forEach((detail) => {
      detail.hidden = detail.dataset.detailId !== row.dataset.detailTarget;
    });
  };

  document.querySelectorAll(".selectable-row").forEach((row) => {
    row.addEventListener("click", () => activateDetail(row));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        activateDetail(row);
      }
    });
  });

  const closeModal = (modal) => {
    modal.hidden = true;
    document.body.style.overflow = "";
  };

  const openModal = (modal) => {
    if (!modal) return;
    modal.hidden = false;
    document.body.style.overflow = "hidden";
    modal.querySelector("[data-modal-close]")?.focus();
  };

  document.querySelectorAll("[data-modal-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const modal = document.querySelector('[data-modal="' + button.dataset.modalOpen + '"]');
      openModal(modal);
    });
  });

  document.querySelectorAll("[data-modal-close]").forEach((button) => {
    button.addEventListener("click", () => closeModal(button.closest("[data-modal]")));
  });

  document.querySelectorAll("[data-modal]").forEach((modal) => {
    modal.addEventListener("click", (event) => {
      if (event.target === modal) closeModal(modal);
    });
  });

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const modal = document.querySelector("[data-modal]:not([hidden])");
    if (modal) closeModal(modal);
  });

  document.querySelectorAll("[data-segment]").forEach((button) => {
    button.addEventListener("click", () => {
      button.parentElement.querySelectorAll("[data-segment]").forEach((candidate) => {
        candidate.classList.toggle("is-active", candidate === button);
      });
    });
  });

  document.querySelectorAll('[aria-disabled="true"]').forEach((control) => {
    control.addEventListener("click", (event) => event.preventDefault());
  });

  const syncStaticParams = (form) => {
    const hidden = form.querySelector("[data-static-params-json]");
    if (!hidden || hidden.disabled) return;
    const values = {};
    form.querySelectorAll("[data-param-rows] .parameter-row").forEach((row) => {
      const name = row.querySelector("[data-param-name]")?.value.trim() || "";
      const value = row.querySelector("[data-param-value]")?.value || "";
      if (!name && !value) return;
      if (!name) throw new Error("파라미터명을 입력하세요.");
      if (Object.hasOwn(values, name)) throw new Error("같은 파라미터명이 두 번 입력되었습니다: " + name);
      values[name] = value;
    });
    hidden.value = JSON.stringify(values);
  };

  const jsonFromForm = (form) => {
    syncStaticParams(form);
    const payload = {};
    const assign = (path, value) => {
      const parts = path.split(".");
      let target = payload;
      parts.slice(0, -1).forEach((part) => { target = target[part] ||= {}; });
      target[parts.at(-1)] = value;
    };
    form.querySelectorAll("[name]:not(:disabled):not([data-ui-only])").forEach((control) => {
      if (control.type === "file") return;
      let value = control.type === "checkbox" ? control.checked : control.value.trim();
      if (control.dataset.linesField !== undefined) {
        value = control.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
      }
      if (control.dataset.jsonField !== undefined) {
        try { value = JSON.parse(value || "{}"); }
        catch (_error) { throw new Error("JSON 설정 형식을 확인하세요."); }
      }
      assign(control.name, value === "" ? null : value);
    });
    return payload;
  };

  document.querySelectorAll("form [data-method-selector]").forEach((selector) => {
    const form = selector.form;
    form.querySelectorAll("[data-method-choice]").forEach((choice) => {
      choice.addEventListener("change", () => {
        if (!choice.checked) return;
        selector.value = choice.value;
        selector.dispatchEvent(new Event("change"));
      });
    });
    const selectedPanel = () => form.querySelector('[data-method-panel="' + selector.value + '"]');
    const editUrl = form.closest("[data-modal]")?.previousElementSibling?.querySelector(".url-cell")?.textContent?.trim();
    if (editUrl) selectedPanel()?.querySelectorAll('[name="url"]').forEach((input) => { input.value = editUrl; });
    try {
      const initial = JSON.parse(form.querySelector("[data-initial-method-config]")?.value || "{}");
      const applyInitial = (prefix, values) => Object.entries(values || {}).forEach(([key, value]) => {
        const name = prefix + "." + key;
        if (value && typeof value === "object" && !Array.isArray(value) && key !== "static_params") {
          applyInitial(name, value);
          return;
        }
        const control = selectedPanel()?.querySelector('[name="' + name + '"]');
        if (!control || value === null) return;
        if (control.type === "checkbox") control.checked = Boolean(value);
        else if (control.dataset.linesField !== undefined && Array.isArray(value)) control.value = value.join("\n");
        else control.value = control.dataset.jsonField !== undefined ? JSON.stringify(value) : String(value);
      });
      applyInitial("method_config", initial);
    } catch (_error) { /* server-owned projection stays authoritative */ }
    const refresh = () => {
      form.querySelectorAll("[data-method-panel]").forEach((panel) => {
        const active = panel.dataset.methodPanel === selector.value;
        panel.hidden = !active;
        panel.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !active; });
      });
      const apiActive = selector.value === "API";
      const apiMode = form.querySelector("[data-api-add-mode]:checked")?.value || "DIRECT";
      form.querySelectorAll("[data-api-mode-panel]").forEach((panel) => {
        const active = apiActive && panel.dataset.apiModePanel === apiMode;
        panel.hidden = !active;
        panel.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !active; });
      });
      const wizardSubmit = form.querySelector("[data-wizard-submit]");
      const selectedCatalog = form.querySelector("[data-selected-catalog]")?.value;
      if (wizardSubmit && apiActive && apiMode === "CATALOG") {
        wizardSubmit.disabled = form.dataset.sourceCreate === undefined || !selectedCatalog;
      } else if (wizardSubmit && form.dataset.sourceCreate === undefined) {
        wizardSubmit.disabled = false;
      } else if (wizardSubmit && selector.value === "WEB_PAGE") {
        wizardSubmit.disabled = form.dataset.scrapePreviewReady !== "true";
      } else if (wizardSubmit) {
        wizardSubmit.disabled = false;
      }
      const kind = form.querySelector("[data-api-kind]");
      const kindChoice = form.querySelector("[data-api-kind-choice]:checked")?.value || "OPEN_API";
      const feedKind = form.querySelector("[data-feed-kind]");
      if (kind) kind.value = kindChoice === "FEED" ? (feedKind?.value || "RSS") : "OPEN_API";
      form.querySelectorAll("[data-api-subtype]").forEach((panel) => {
        const active = apiActive && apiMode === "DIRECT" && panel.dataset.apiSubtype === kindChoice;
        panel.hidden = !active;
        panel.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !active; });
      });
      const auth = form.querySelector("[data-api-auth]");
      const credentialFields = form.querySelectorAll("[data-credential-fields]");
      if (auth && credentialFields.length) {
        const active = apiActive && apiMode === "DIRECT" && kindChoice === "OPEN_API" && auth.value !== "NONE";
        credentialFields.forEach((credential) => {
          credential.hidden = !active;
          credential.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !active; });
        });
      }
      const pagination = form.querySelector("[data-pagination-mode]");
      const paginationFields = form.querySelector("[data-pagination-fields]");
      if (pagination && paginationFields) {
        const active = apiActive && apiMode === "DIRECT" && kindChoice === "OPEN_API" && pagination.value === "PAGE_NUMBER";
        paginationFields.hidden = !active;
        paginationFields.querySelectorAll("input,select,textarea,button").forEach((control) => { control.disabled = !active; });
      }
      form.dispatchEvent(new CustomEvent("methodrefresh"));
    };
    selector.addEventListener("change", refresh);
    form.querySelectorAll("[data-api-add-mode], [data-api-kind-choice]").forEach((control) => control.addEventListener("change", refresh));
    form.querySelector("[data-feed-kind]")?.addEventListener("change", refresh);
    form.querySelector("[data-api-auth]")?.addEventListener("change", refresh);
    form.querySelector("[data-pagination-mode]")?.addEventListener("change", refresh);
    refresh();
  });

  document.querySelectorAll("[data-source-wizard]").forEach((form) => {
    let step = 1;
    const selector = form.querySelector("[data-method-selector]");
    const scopeControls = [...form.querySelectorAll("[data-binding-scope]")];
    const orgField = form.querySelector("[data-org-scope-field]");
    const submit = form.querySelector("[data-wizard-submit]");
    const previewControls = form.querySelector("[data-scrape-preview-controls]");
    const previewResult = form.querySelector("[data-scrape-preview-result]");
    const syncConnection = () => {
      const scope = form.querySelector("[data-binding-scope]:checked")?.value || "AGENCY_WIDE";
      const specific = scope === "SPECIFIC_ORG_UNIT";
      if (orgField) {
        orgField.hidden = !specific;
        orgField.querySelectorAll("input,button").forEach((control) => {
          control.disabled = !specific || (control.matches("[data-lookup-input]") && !form.querySelector('[name="agency_id"]')?.value);
        });
        if (!specific) {
          const orgValue = orgField.querySelector('[name="org_unit_id"]');
          const orgInput = orgField.querySelector("[data-lookup-input]");
          if (orgValue) orgValue.value = "";
          if (orgInput) orgInput.value = "";
        }
      }
      const guidance = form.querySelector("[data-method-guidance]");
      if (guidance) guidance.textContent = selector.value === "WEB_CRAWL"
        ? "여러 부서 정보를 탐색하는 크롤링 소스라면 기관 전체 연결을 권장합니다."
        : selector.value === "WEB_PAGE"
          ? "특정 부서 전용 페이지라면 특정 부서를 선택할 수 있습니다."
          : "기관 전체 또는 실제 endpoint가 대표하는 특정 부서에 연결하세요.";
      if (previewControls) previewControls.hidden = selector.value !== "WEB_PAGE";
      if (form.dataset.sourceCreate !== undefined && selector.value !== "WEB_PAGE") {
        const catalogPending = selector.value === "API"
          && form.querySelector("[data-api-add-mode]:checked")?.value === "CATALOG"
          && !form.querySelector("[data-selected-catalog]")?.value;
        submit.disabled = catalogPending;
      }
    };
    const invalidateScrapePreview = () => {
      if (form.dataset.sourceCreate === undefined || selector.value !== "WEB_PAGE") return;
      form.dataset.scrapePreviewReady = "";
      if (previewResult) previewResult.hidden = true;
      if (submit) submit.disabled = true;
    };
    const showStep = (nextStep) => {
      step = nextStep;
      form.querySelectorAll("[data-wizard-step]").forEach((section) => { section.hidden = Number(section.dataset.wizardStep) !== step; });
      form.querySelectorAll("[data-wizard-indicator]").forEach((item) => item.classList.toggle("is-active", Number(item.dataset.wizardIndicator) === step));
      form.querySelector("[data-wizard-prev]").hidden = step === 1;
      form.querySelector("[data-wizard-next]").hidden = step === 3;
      submit.hidden = step !== 3;
      syncConnection();
      if (step === 3 && form.dataset.sourceCreate !== undefined && selector.value === "WEB_PAGE") {
        submit.disabled = form.dataset.scrapePreviewReady !== "true";
      }
    };
    const validateStep = () => {
      const active = form.querySelector('[data-wizard-step="' + step + '"]');
      const kind = active?.dataset.wizardKind;
      if (kind === "connection") {
        const agencyValue = form.querySelector('[name="agency_id"]');
        const agencyInput = agencyValue?.closest("[data-lookup-combobox]")?.querySelector("[data-lookup-input]");
        agencyInput?.setCustomValidity(agencyValue?.value ? "" : "기관을 목록에서 선택하세요.");
        const specific = form.querySelector("[data-binding-scope]:checked")?.value === "SPECIFIC_ORG_UNIT";
        const orgValue = form.querySelector('[name="org_unit_id"]');
        const orgInput = orgValue?.closest("[data-lookup-combobox]")?.querySelector("[data-lookup-input]");
        orgInput?.setCustomValidity(!specific || orgValue?.value ? "" : "부서를 목록에서 선택하세요.");
      }
      if (kind === "config" && ["WEB_PAGE", "WEB_CRAWL"].includes(selector.value)) {
        const panel = form.querySelector('[data-method-panel="' + selector.value + '"]');
        const checks = [...panel.querySelectorAll('[name^="method_config.extract_"]')];
        checks[0]?.setCustomValidity(checks.some((control) => control.checked) ? "" : "가져올 정보를 하나 이상 선택하세요.");
        const urls = panel.querySelector("[data-scrape-urls]");
        if (urls) {
          const count = urls.value.split(/\r?\n/).filter((value) => value.trim()).length;
          urls.setCustomValidity(count === 0 ? "대상 URL을 입력하세요." : count > 200 ? "한 번에 최대 200개까지 입력할 수 있습니다. 더 큰 목록은 엑셀 업로드를 사용하세요." : "");
        }
      }
      if (kind === "config" && selector.value === "API" && form.querySelector("[data-api-add-mode]:checked")?.value === "CATALOG") {
        const selected = form.querySelector("[data-selected-catalog]");
        const catalogChoice = form.querySelector('[data-api-add-mode][value="CATALOG"]');
        catalogChoice?.setCustomValidity(selected?.value ? "" : "기본 공개 소스를 선택하세요.");
      }
      const invalid = active?.querySelector(":invalid");
      if (invalid) { invalid.reportValidity(); return false; }
      return true;
    };
    form.querySelector("[data-wizard-next]").addEventListener("click", () => { if (validateStep()) showStep(step + 1); });
    form.querySelector("[data-wizard-prev]").addEventListener("click", () => showStep(step - 1));
    form.addEventListener("submit", (event) => { if (!validateStep()) event.preventDefault(); }, {capture: true});
    scopeControls.forEach((control) => control.addEventListener("change", () => { syncConnection(); invalidateScrapePreview(); }));
    form.addEventListener("methodrefresh", () => { syncConnection(); invalidateScrapePreview(); });
    form.addEventListener("input", (event) => {
      if (!event.target.closest("[data-scrape-preview-result]")) invalidateScrapePreview();
    });
    form.addEventListener("change", (event) => {
      if (event.target.matches("[data-lookup-value]")) invalidateScrapePreview();
    });
    showStep(1);
  });

  const addParameterRow = (container, name = "", value = "") => {
    const row = document.createElement("div");
    row.className = "parameter-row";
    const nameInput = document.createElement("input");
    nameInput.className = "control"; nameInput.placeholder = "파라미터명";
    nameInput.dataset.paramName = ""; nameInput.dataset.uiOnly = ""; nameInput.value = name;
    const valueInput = document.createElement("input");
    valueInput.className = "control"; valueInput.placeholder = "값";
    valueInput.dataset.paramValue = ""; valueInput.dataset.uiOnly = ""; valueInput.value = value;
    const remove = document.createElement("button");
    remove.className = "button button--ghost"; remove.type = "button";
    remove.dataset.paramRemove = ""; remove.textContent = "삭제";
    remove.addEventListener("click", () => row.remove());
    row.append(nameInput, valueInput, remove);
    container.append(row);
  };
  document.querySelectorAll("[data-param-rows]").forEach((container) => {
    container.querySelectorAll("[data-param-remove]").forEach((button) => button.addEventListener("click", () => button.closest(".parameter-row")?.remove()));
    if (!container.children.length) addParameterRow(container);
    container.closest(".parameter-editor")?.querySelector("[data-param-add]")?.addEventListener("click", () => addParameterRow(container));
  });

  document.querySelectorAll("[data-catalog-select]").forEach((button) => {
    button.addEventListener("click", () => {
      const form = button.form;
      const selected = form.querySelector("[data-selected-catalog]");
      selected.value = button.dataset.catalogSelect;
      form.querySelectorAll("[data-catalog-select]").forEach((candidate) => candidate.classList.toggle("is-selected", candidate === button));
      form.querySelector('[data-api-add-mode][value="CATALOG"]')?.setCustomValidity("");
      form.querySelector("[data-wizard-submit]").disabled = false;
    });
  });

  document.querySelectorAll("[data-config-preview]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const output = button.parentElement.querySelector("[data-preview-result]") || form.querySelector("[data-preview-result]");
      button.disabled = true;
      try {
        const payload = jsonFromForm(form);
        const response = await fetch("/api/source-config/preview", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify({url: payload.url, method_config: payload.method_config}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "설정을 확인하지 못했습니다.");
        output.textContent = "HTTP " + result.http_status + " · 원시 " + result.raw_count + "건 · 표본 " + result.mapped_sample.length + "건";
      } catch (error) {
        output.textContent = error.message;
      } finally {
        if (output) output.hidden = false;
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-credential-save]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const secret = form.querySelector("[data-credential-secret]")?.value || "";
      const ref = form.querySelector('[name="method_config.credential_ref"]');
      const output = button.closest("[data-credential-fields]").querySelector("[data-credential-result]");
      try {
        const response = await fetch("/api/api-credentials", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify({secret_value: secret, credential_ref: ref?.value || null}),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "자격증명을 저장하지 못했습니다.");
        if (ref) ref.value = result.credential_ref;
        form.querySelector("[data-credential-secret]").value = "";
        output.textContent = "자격증명이 저장되었습니다. 비밀 값은 다시 표시되지 않습니다.";
      } catch (error) { output.textContent = error.message; }
      output.hidden = false;
    });
  });

  document.querySelectorAll("[data-credential-remove]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const ref = form.querySelector('[name="method_config.credential_ref"]');
      const output = button.closest("[data-credential-fields]").querySelector("[data-credential-result]");
      if (!ref?.value) { output.textContent = "삭제할 자격증명이 없습니다."; output.hidden = false; return; }
      const response = await fetch("/api/api-credentials/" + encodeURIComponent(ref.value), {
        method: "DELETE", headers: csrfHeaders(),
      });
      if (response.ok) { ref.value = ""; output.textContent = "자격증명을 제거했습니다."; }
      else { output.textContent = "자격증명을 제거하지 못했습니다."; }
      output.hidden = false;
    });
  });

  document.querySelectorAll("[data-catalog-use]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const agencyId = form.querySelector('[name="agency_id"]')?.value;
      const orgUnitId = form.querySelector('[name="org_unit_id"]')?.value || null;
      if (!agencyId) { window.alert("기본 공개 소스를 사용할 기관을 선택하세요."); return; }
      const response = await fetch("/api/public-source-catalog/" + encodeURIComponent(button.dataset.catalogUse) + "/activate", {
        method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
        body: JSON.stringify({agency_id: agencyId, org_unit_id: orgUnitId}),
      });
      const result = await response.json();
      if (!response.ok) { window.alert(result.detail || "기본 공개 소스를 활성화하지 못했습니다."); return; }
      window.location.reload();
    });
  });

  const scrapeBatchPayload = (form) => {
    const values = jsonFromForm(form);
    return {
      urls: values.urls,
      agency_id: values.agency_id,
      org_unit_id: values.org_unit_id,
      binding_scope: form.querySelector("[data-binding-scope]:checked")?.value || "AGENCY_WIDE",
      description: values.description,
      method_config: values.method_config || {},
      scheduled_refresh_enabled: values.scheduled_refresh_enabled,
    };
  };
  const scrapeLabels = {
    NEW_SOURCE: "신규 소스", EXISTING_SOURCE: "기존 소스",
    DUPLICATE_INPUT: "입력 중복", INVALID_URL: "오류",
    NEW_BINDING: "새 연결", EXACT_BINDING_DUPLICATE: "연결 중복", CONFLICT: "충돌",
  };
  document.querySelectorAll("[data-scrape-preview]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const output = form.querySelector("[data-scrape-preview-result]");
      const errorBox = form.querySelector("[data-form-error]");
      button.disabled = true;
      try {
        const payload = scrapeBatchPayload(form);
        if (!payload.agency_id) throw new Error("기관을 검색 결과에서 선택하세요.");
        if (payload.binding_scope === "SPECIFIC_ORG_UNIT" && !payload.org_unit_id) throw new Error("특정 부서를 선택하세요.");
        const response = await fetch("/api/source-bindings/scrape-batch/preview", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify(payload),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "등록 대상을 미리 보지 못했습니다.");
        const summary = result.summary;
        form.querySelector("[data-scrape-summary]").innerHTML = [
          ["입력 URL", summary.input_urls], ["신규 소스", summary.new_sources],
          ["기존 소스", summary.existing_sources], ["중복 입력", summary.duplicate_input],
          ["오류", summary.invalid_urls + summary.conflicts], ["새 연결", summary.new_bindings],
        ].map(([label, value]) => "<span><strong>" + value + "</strong><small>" + label + "</small></span>").join("");
        const rows = form.querySelector("[data-scrape-preview-rows]");
        rows.replaceChildren();
        result.rows.forEach((item) => {
          const tr = document.createElement("tr");
          [item.line_number, item.raw_url, scrapeLabels[item.classification] || item.classification,
            scrapeLabels[item.binding_classification] || item.binding_classification || "-", item.message]
            .forEach((value) => { const td = document.createElement("td"); td.textContent = value; tr.append(td); });
          rows.append(tr);
        });
        output.hidden = false;
        form.dataset.scrapePreviewReady = "true";
        form.querySelector("[data-wizard-submit]").disabled = summary.importable === 0;
        errorBox.hidden = true;
      } catch (error) {
        form.dataset.scrapePreviewReady = "";
        form.querySelector("[data-wizard-submit]").disabled = true;
        errorBox.textContent = error.message; errorBox.hidden = false;
      } finally { button.disabled = false; }
    });
  });

  document.querySelectorAll("[data-api-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      const errorBox = form.querySelector("[data-form-error]");
      try {
        const method = form.querySelector("[data-method-selector]")?.value;
        if (form.dataset.sourceCreate !== undefined && method === "WEB_PAGE") {
          if (form.dataset.scrapePreviewReady !== "true") throw new Error("등록 전에 서버 미리보기를 확인하세요.");
          const response = await fetch("/api/source-bindings/scrape-batch/confirm", {
            method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
            body: JSON.stringify(scrapeBatchPayload(form)),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "URL 일괄 등록을 완료하지 못했습니다.");
          const labels = [
            ["입력", result.summary.input], ["신규 Source", result.summary.created_sources],
            ["기존 Source 재사용", result.summary.existing_sources_reused],
            ["새 연결", result.summary.created_bindings], ["중복 건너뜀", result.summary.duplicates_skipped],
            ["오류", result.summary.errors],
          ];
          const summary = form.querySelector("[data-scrape-result-summary]");
          summary.replaceChildren();
          labels.forEach(([label, value]) => {
            const dt = document.createElement("dt"); dt.textContent = label;
            const dd = document.createElement("dd"); dd.textContent = value + "개";
            summary.append(dt, dd);
          });
          form.dataset.createdSourceIds = JSON.stringify(result.source_ids || []);
          form.querySelector("[data-connection-fields]").hidden = true;
          form.querySelector("[data-scrape-result]").hidden = false;
          form.querySelector(".modal-actions--sticky").hidden = true;
          form.querySelector("[data-form-error]").hidden = true;
          return;
        }
        if (form.dataset.sourceCreate !== undefined && method === "API" && form.querySelector("[data-api-add-mode]:checked")?.value === "CATALOG") {
          const catalogId = form.querySelector("[data-selected-catalog]")?.value;
          const values = jsonFromForm(form);
          if (!catalogId) throw new Error("기본 공개 소스를 선택하세요.");
          const response = await fetch("/api/public-source-catalog/" + encodeURIComponent(catalogId) + "/activate", {
            method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
            body: JSON.stringify({agency_id: values.agency_id, org_unit_id: values.org_unit_id}),
          });
          const result = await response.json();
          if (!response.ok) throw new Error(result.detail || "기본 공개 소스를 등록하지 못했습니다.");
          window.location.reload();
          return;
        }
        const response = await fetch(form.action, {
          method: form.dataset.method || "POST",
          headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify(jsonFromForm(form)),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "요청을 처리하지 못했습니다.");
        window.location.reload();
      } catch (error) {
        if (errorBox) {
          errorBox.textContent = error.message;
          errorBox.hidden = false;
        }
      }
    });
  });

  document.querySelectorAll("[data-api-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const response = await fetch(button.dataset.apiAction, {
        method: "POST",
        headers: csrfHeaders({"Content-Type": "application/json"}),
        body: button.dataset.body || "{}",
      });
      if (response.ok) window.location.reload();
    });
  });

  const jobStatusText = (job) => {
    const progress = job.completed_items + "/" + job.total_items;
    if (job.status === "PENDING") return "수집 작업 대기 중 · " + progress;
    if (job.status === "RUNNING") return "수집 진행 중 · " + progress;
    if (job.status === "COMPLETED") return "수집 완료 · " + progress;
    if (job.status === "COMPLETED_WITH_ERRORS") return "오류 포함 완료 · 성공 " + job.succeeded_items + " / 실패 " + job.failed_items;
    return "수집 작업 " + job.status + " · " + progress;
  };

  const pollCollectionJob = (job, output, onTerminal) => {
    if (output) { output.hidden = false; output.textContent = jobStatusText(job); }
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/api/collection-jobs/" + job.id);
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "수집 작업 상태를 확인하지 못했습니다.");
        if (output) { output.hidden = false; output.textContent = jobStatusText(result.job); }
        if (["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"].includes(result.job.status)) {
          window.clearInterval(timer);
          if (onTerminal) onTerminal(result.job);
        }
      } catch (error) {
        window.clearInterval(timer);
        if (output) output.textContent = error.message;
      }
    }, 3000);
  };

  const createCollectionJob = async (payload, output) => {
    const response = await fetch("/api/collection-jobs", {
      method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
      body: JSON.stringify(payload),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || "수집 작업을 만들지 못했습니다.");
    pollCollectionJob(result.job, output, () => window.location.reload());
    return result.job;
  };

  document.querySelectorAll("[data-scrape-list]").forEach((button) => {
    button.addEventListener("click", () => window.location.reload());
  });
  document.querySelectorAll("[data-scrape-collect]").forEach((button) => {
    button.addEventListener("click", async () => {
      const form = button.form;
      const output = form.querySelector("[data-scrape-job-status]");
      let sourceIds = [];
      try { sourceIds = JSON.parse(form.dataset.createdSourceIds || "[]"); }
      catch (_error) { /* handled by the empty check */ }
      if (!sourceIds.length) { output.textContent = "수집할 신규 연결이 없습니다."; output.hidden = false; return; }
      button.disabled = true;
      try {
        await createCollectionJob({trigger_type: "MANUAL_SELECTION", source_ids: sourceIds}, output);
      } catch (error) {
        output.textContent = error.message; output.hidden = false; button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-collect-source]").forEach((button) => {
    button.addEventListener("click", async () => {
      const sourceId = button.dataset.collectSource;
      const related = document.querySelectorAll('[data-collect-source="' + sourceId + '"]');
      const errorBox = button.closest(".detail-content")?.querySelector("[data-collect-error]");
      related.forEach((item) => {
        item.dataset.collectWasDisabled = String(item.disabled);
        item.disabled = true;
        item.dataset.originalText ||= item.textContent;
        item.textContent = "수집 중";
      });
      if (errorBox) errorBox.hidden = true;
      try {
        const response = await fetch("/api/sources/" + sourceId + "/collect", {method: "POST", headers: csrfHeaders()});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "수집 작업을 만들지 못했습니다.");
        pollCollectionJob(result.job, errorBox, () => window.location.reload());
      } catch (error) {
        related.forEach((item) => {
          item.disabled = item.dataset.collectWasDisabled === "true";
          item.textContent = item.dataset.originalText || "수집";
          delete item.dataset.collectWasDisabled;
        });
        if (errorBox) {
          errorBox.textContent = error.message;
          errorBox.hidden = false;
        }
      }
    });
  });

  const sharedJobStatus = document.querySelector("[data-job-status]");
  const selectionButton = document.querySelector("[data-job-selection]");
  const selectedSourceIds = () => [...new Set([...document.querySelectorAll("[data-source-select]:checked")].map((item) => item.value))];
  const updateSelectionState = () => {
    if (selectionButton) selectionButton.disabled = selectedSourceIds().length === 0;
    const pageSelector = document.querySelector("[data-source-select-page]");
    const available = [...document.querySelectorAll("[data-source-select]:not(:disabled)")];
    if (pageSelector) {
      pageSelector.checked = available.length > 0 && available.every((item) => item.checked);
      pageSelector.indeterminate = available.some((item) => item.checked) && !pageSelector.checked;
    }
  };
  document.querySelectorAll("[data-source-select]").forEach((item) => {
    item.addEventListener("click", (event) => event.stopPropagation());
    item.addEventListener("change", updateSelectionState);
  });
  document.querySelector("[data-source-select-page]")?.addEventListener("change", (event) => {
    document.querySelectorAll("[data-source-select]:not(:disabled)").forEach((item) => { item.checked = event.currentTarget.checked; });
    updateSelectionState();
  });
  updateSelectionState();
  selectionButton?.addEventListener("click", async (event) => {
    const sourceIds = selectedSourceIds();
    if (!sourceIds.length) { sharedJobStatus.textContent = "수집할 소스를 선택하세요."; return; }
    if (sourceIds.length > 20 && !window.confirm(sourceIds.length + "개 소스를 수집하시겠습니까?")) return;
    event.currentTarget.disabled = true;
    try { await createCollectionJob({trigger_type: "MANUAL_SELECTION", source_ids: sourceIds}, sharedJobStatus); }
    catch (error) { sharedJobStatus.textContent = error.message; event.currentTarget.disabled = false; }
  });
  const sourceFilterSnapshot = () => {
    try { return JSON.parse(document.querySelector("#source-filter-state")?.textContent || "{}"); }
    catch (_error) { throw new Error("필터 상태를 읽지 못했습니다."); }
  };
  const filterPreviewUrl = () => {
    const filter = sourceFilterSnapshot();
    const params = new URLSearchParams();
    if (filter.search) params.set("search", filter.search);
    (filter.region_codes || []).forEach((value) => params.append("region_code", value));
    if (filter.agency_id) params.set("agency_id", filter.agency_id);
    if (filter.org_unit_id) params.set("org_unit_id", filter.org_unit_id);
    if (filter.methods !== null && filter.methods !== undefined) params.set("methods", filter.methods.join(","));
    if (filter.status) params.set("source_status", filter.status);
    if (filter.scheduled) params.set("scheduled", filter.scheduled);
    return "/api/source-index/preview?" + params.toString();
  };
  const openBulkConfirmation = async (scope) => {
    const modal = document.querySelector('[data-modal="' + (scope === "all" ? "all-job-confirm" : "filtered-job-confirm") + '"]');
    const trigger = document.querySelector(scope === "all" ? "[data-job-all-open]" : "[data-job-filtered-open]");
    const confirm = modal?.querySelector(scope === "all" ? "[data-job-all-confirm]" : "[data-job-filter-confirm]");
    const errorBox = modal?.querySelector("[data-bulk-error]");
    trigger.disabled = true;
    try {
      const response = await fetch(scope === "all" ? "/api/source-index/preview?scope=all" : filterPreviewUrl());
      const preview = await response.json();
      if (!response.ok) throw new Error(preview.detail || "수집 대상을 확인하지 못했습니다.");
      const counts = preview.method_counts || {};
      modal.querySelector("[data-bulk-total]").textContent = preview.eligible_total + "개";
      modal.querySelector("[data-bulk-web-page]").textContent = String(counts.WEB_PAGE || 0);
      modal.querySelector("[data-bulk-web-crawl]").textContent = String(counts.WEB_CRAWL || 0);
      modal.querySelector("[data-bulk-api]").textContent = String(counts.API || 0);
      confirm.textContent = "수집 시작";
      confirm.disabled = preview.eligible_total === 0;
      errorBox.hidden = true;
      openModal(modal);
    } catch (error) {
      sharedJobStatus.hidden = false;
      sharedJobStatus.textContent = error.message;
    } finally {
      trigger.disabled = false;
    }
  };
  document.querySelector("[data-job-filtered-open]")?.addEventListener("click", () => openBulkConfirmation("filter"));
  document.querySelector("[data-job-all-open]")?.addEventListener("click", () => openBulkConfirmation("all"));
  document.querySelector("[data-job-all-confirm]")?.addEventListener("click", async (event) => {
    event.currentTarget.disabled = true;
    try {
      closeModal(event.currentTarget.closest("[data-modal]"));
      await createCollectionJob({trigger_type: "MANUAL_ALL"}, sharedJobStatus);
    } catch (error) {
      sharedJobStatus.hidden = false; sharedJobStatus.textContent = error.message; event.currentTarget.disabled = false;
    }
  });
  document.querySelector("[data-job-filter-confirm]")?.addEventListener("click", async (event) => {
    let filter;
    try { filter = sourceFilterSnapshot(); }
    catch (_error) { sharedJobStatus.hidden = false; sharedJobStatus.textContent = "필터 상태를 읽지 못했습니다."; return; }
    event.currentTarget.disabled = true;
    try {
      closeModal(event.currentTarget.closest("[data-modal]"));
      await createCollectionJob({trigger_type: "MANUAL_FILTER", filter}, sharedJobStatus);
    } catch (error) {
      sharedJobStatus.hidden = false;
      sharedJobStatus.textContent = error.message;
      event.currentTarget.disabled = false;
    }
  });
  document.querySelectorAll("[data-job-agency]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    const output = button.closest(".detail-content")?.querySelector("[data-job-status]");
    try { await createCollectionJob({trigger_type: "MANUAL_AGENCY", agency_id: button.dataset.jobAgency}, output); }
    catch (error) { if (output) output.textContent = error.message; button.disabled = false; }
  }));
  document.querySelectorAll("[data-job-org-unit]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    const output = button.closest(".detail-content")?.querySelector("[data-job-status]");
    try { await createCollectionJob({trigger_type: "MANUAL_ORG_UNIT", org_unit_id: button.dataset.jobOrgUnit}, output); }
    catch (error) { if (output) output.textContent = error.message; button.disabled = false; }
  }));
  const liveJobRows = [...document.querySelectorAll("[data-job-summary]")];
  if (liveJobRows.some((row) => ["PENDING", "RUNNING"].includes(row.querySelector("[data-job-state]")?.dataset.jobStateCode))) {
    window.setInterval(async () => {
      await Promise.all(liveJobRows.map(async (row) => {
        const response = await fetch("/api/collection-jobs/" + row.dataset.jobSummary);
        if (!response.ok) return;
        const job = (await response.json()).job;
        row.querySelector("[data-job-progress]").textContent = job.completed_items + " / " + job.total_items + " (" + job.progress_percent + "%)";
        row.querySelector("[data-job-success]").textContent = job.succeeded_items;
        row.querySelector("[data-job-failed]").textContent = job.failed_items;
        const labels = {PENDING: "대기", RUNNING: "진행 중", COMPLETED: "완료", COMPLETED_WITH_ERRORS: "오류 포함 완료", FAILED: "실패", CANCELLED: "취소"};
        row.querySelector("[data-job-state]").textContent = labels[job.status] || job.status;
        row.querySelector("[data-job-state]").dataset.jobStateCode = job.status;
      }));
    }, 3000);
  }

  document.querySelectorAll("[data-method-filter]").forEach((control) => {
    control.addEventListener("change", () => {
      const hidden = control.closest("form").querySelector("[data-method-filter-value]");
      hidden.value = [...control.closest("fieldset").querySelectorAll("[data-method-filter]:checked")].map((item) => item.value).join(",");
    });
  });

  const clearLookup = (root, notify = true) => {
    const input = root.querySelector("[data-lookup-input]");
    const value = root.querySelector("[data-lookup-value]");
    input.value = "";
    value.value = "";
    input.setAttribute("aria-expanded", "false");
    root.querySelector("[data-lookup-results]").hidden = true;
    root.querySelector("[data-lookup-clear]").hidden = true;
    if (notify) value.dispatchEvent(new Event("change", {bubbles: true}));
  };

  document.querySelectorAll("[data-lookup-combobox]").forEach((root) => {
    const input = root.querySelector("[data-lookup-input]");
    const value = root.querySelector("[data-lookup-value]");
    const results = root.querySelector("[data-lookup-results]");
    const clear = root.querySelector("[data-lookup-clear]");
    const form = root.closest("form");
    let timer;
    let controller;
    let activeIndex = -1;
    const agencyValue = () => form?.querySelector('[name="agency_id"]')?.value || "";
    const close = () => { results.hidden = true; input.setAttribute("aria-expanded", "false"); activeIndex = -1; };
    const selectItem = (item) => {
      input.value = item.label;
      value.value = item.id;
      clear.hidden = false;
      input.setCustomValidity("");
      close();
      value.dispatchEvent(new Event("change", {bubbles: true}));
    };
    const render = (items) => {
      results.replaceChildren();
      items.forEach((item, index) => {
        const option = document.createElement("button");
        option.type = "button";
        option.setAttribute("role", "option");
        option.dataset.lookupOption = String(index);
        option.textContent = item.label;
        option.addEventListener("mousedown", (event) => { event.preventDefault(); selectItem(item); });
        results.append(option);
      });
      results.hidden = items.length === 0;
      input.setAttribute("aria-expanded", String(items.length > 0));
      activeIndex = -1;
    };
    const search = async () => {
      const query = input.value.trim();
      if (root.dataset.lookupKind === "agency" && !query) { close(); return; }
      const agencyId = agencyValue();
      if (root.dataset.lookupKind === "org" && !agencyId) { close(); return; }
      controller?.abort();
      controller = new AbortController();
      const params = new URLSearchParams({q: query, limit: "30"});
      if (root.dataset.lookupKind === "agency") {
        const region = form?.querySelector('[name="region_code"]')?.value;
        if (region) params.set("region_code", region);
      }
      const endpoint = root.dataset.lookupKind === "agency"
        ? "/api/lookups/agencies?" + params
        : "/api/lookups/agencies/" + encodeURIComponent(agencyId) + "/org-units?" + params;
      try {
        const response = await fetch(endpoint, {signal: controller.signal});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "검색 결과를 불러오지 못했습니다.");
        render(result.items);
      } catch (error) {
        if (error.name !== "AbortError") render([]);
      }
    };
    input.addEventListener("input", () => {
      if (value.value) {
        value.value = "";
        value.dispatchEvent(new Event("change", {bubbles: true}));
      }
      clear.hidden = !input.value;
      window.clearTimeout(timer);
      timer = window.setTimeout(search, 250);
    });
    input.addEventListener("focus", () => { if (root.dataset.lookupKind === "org" || input.value) search(); });
    input.addEventListener("blur", () => {
      window.setTimeout(close, 100);
      if (input.required && !value.value) input.setCustomValidity("검색 결과에서 기관을 선택하세요.");
    });
    input.addEventListener("keydown", (event) => {
      const options = [...results.querySelectorAll("[data-lookup-option]")];
      if (event.key === "Escape") { close(); return; }
      if (!options.length || !["ArrowDown", "ArrowUp", "Enter"].includes(event.key)) return;
      event.preventDefault();
      if (event.key === "Enter" && activeIndex >= 0) { options[activeIndex].dispatchEvent(new MouseEvent("mousedown")); return; }
      activeIndex = event.key === "ArrowDown" ? Math.min(activeIndex + 1, options.length - 1) : Math.max(activeIndex - 1, 0);
      options.forEach((option, index) => option.setAttribute("aria-selected", String(index === activeIndex)));
    });
    clear.addEventListener("click", () => { clearLookup(root); input.focus(); });
    clear.hidden = !value.value;
  });

  document.querySelectorAll('input[name="agency_id"][data-lookup-value]').forEach((agencyValue) => {
    const form = agencyValue.form;
    const orgRoot = form?.querySelector('[data-lookup-kind="org"]');
    const sync = () => {
      if (!orgRoot) return;
      const orgInput = orgRoot.querySelector("[data-lookup-input]");
      const orgValue = orgRoot.querySelector("[data-lookup-value]");
      if (orgValue.value && orgValue.dataset.agencyId && orgValue.dataset.agencyId !== agencyValue.value) clearLookup(orgRoot, false);
      orgInput.disabled = !agencyValue.value || Boolean(orgRoot.closest("[data-org-scope-field]")?.hidden);
      if (!agencyValue.value && orgValue.value) clearLookup(orgRoot, false);
    };
    agencyValue.addEventListener("change", () => {
      if (orgRoot) clearLookup(orgRoot, false);
      sync();
    });
    sync();
  });

  const importWorkflow = document.querySelector("[data-import-workflow]");
  if (importWorkflow) {
    const form = importWorkflow.querySelector("[data-import-preview]");
    const errorBox = importWorkflow.querySelector("[data-import-error]");
    const previewBox = importWorkflow.querySelector("[data-import-preview-result]");
    const rowsBox = importWorkflow.querySelector("[data-import-rows]");
    const summaryBox = importWorkflow.querySelector("[data-import-summary]");
    const confirmButton = importWorkflow.querySelector("[data-import-confirm]");
    const completeBox = importWorkflow.querySelector("[data-import-complete]");
    const completeSummary = importWorkflow.querySelector("[data-import-complete-summary]");
    let previewToken = null;
    const labels = {
      READY: "등록 가능", NEW_AGENCY: "신규 기관", NEW_ORG_UNIT: "신규 부서",
      EXACT_DUPLICATE: "중복", EXISTING_SOURCE_NEW_BINDING: "기존 URL 새 연결",
      CONFLICT: "충돌", INVALID: "오류",
    };
    const setStep = (name) => {
      importWorkflow.querySelectorAll("[data-import-step]").forEach((step) => {
        step.classList.toggle("is-active", step.dataset.importStep === name);
      });
    };
    const discardPreview = () => {
      if (!previewToken) return;
      fetch("/api/source-bindings/imports/" + previewToken, {method: "DELETE", headers: csrfHeaders(), keepalive: true});
      previewToken = null;
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      discardPreview();
      errorBox.hidden = true;
      confirmButton.disabled = true;
      setStep("columns");
      try {
        const response = await fetch(form.action, {method: "POST", headers: csrfHeaders(), body: new FormData(form)});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "파일을 확인하지 못했습니다.");
        previewToken = result.token;
        rowsBox.replaceChildren();
        result.rows.forEach((row) => {
          const tr = document.createElement("tr");
          [row.row_number, row.resolved_agency || row.raw_agency, row.resolved_org_unit || "기관 공통", row.normalized_url || row.raw_url, labels[row.classification], row.message].forEach((value) => {
            const td = document.createElement("td");
            td.textContent = value;
            tr.appendChild(td);
          });
          rowsBox.appendChild(tr);
        });
        const s = result.summary;
        summaryBox.textContent = "전체 " + s.total + " · 등록 가능 " + s.importable + " · 신규 기관 " + s.new_agencies + " · 신규 부서 " + s.new_org_units + " · 중복 " + s.duplicates + " · 충돌 " + s.conflicts + " · 오류 " + s.invalid;
        previewBox.hidden = false;
        completeBox.hidden = true;
        confirmButton.disabled = s.importable === 0;
        setStep("preview");
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
        previewBox.hidden = true;
        setStep("select");
      }
    });
    confirmButton.addEventListener("click", async () => {
      if (!previewToken) return;
      confirmButton.disabled = true;
      setStep("result");
      try {
        const response = await fetch("/api/source-bindings/imports/" + previewToken + "/confirm", {method: "POST", headers: csrfHeaders()});
        const result = await response.json();
        previewToken = null;
        if (!response.ok) throw new Error(result.detail || "등록을 완료하지 못했습니다.");
        const s = result.summary;
        completeSummary.textContent = "연결 " + s.created_bindings + "건 등록 · 중복 " + s.duplicates_skipped + "건 건너뜀 · 오류 " + (s.invalid_rows + s.conflicts + s.unexpected_failures) + "건";
        previewBox.hidden = true;
        completeBox.hidden = false;
        setStep("complete");
      } catch (error) {
        errorBox.textContent = error.message;
        errorBox.hidden = false;
      }
    });
    importWorkflow.closest("[data-modal]").querySelectorAll("[data-modal-close]").forEach((button) => button.addEventListener("click", discardPreview));
    importWorkflow.querySelector("[data-import-refresh]").addEventListener("click", () => window.location.reload());
  }
  document.querySelectorAll("[data-review-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const errorBox = button.closest(".detail-content")?.querySelector("[data-review-error]");
      button.disabled = true;
      try {
        const response = await fetch("/api/review/" + button.dataset.candidateId + "/" + button.dataset.reviewAction, {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}), body: "{}",
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "검토 작업을 완료하지 못했습니다.");
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        if (errorBox) { errorBox.textContent = error.message; errorBox.hidden = false; }
      }
    });
  });

  document.querySelectorAll("[data-master-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const box = button.closest("[data-master-workflow]");
      const extractionId = box.dataset.extractionId;
      const agencyId = box.querySelector("[data-master-agency]")?.value || "";
      const errorBox = box.querySelector("[data-master-error]");
      button.disabled = true;
      try {
        let action = button.dataset.masterAction;
        if (action === "resolve") {
          if (!agencyId) throw new Error("대상 기관을 선택하세요.");
          const stateResponse = await fetch("/api/extractions/" + extractionId + "/workflow?agency_id=" + encodeURIComponent(agencyId));
          const state = await stateResponse.json();
          if (!stateResponse.ok) throw new Error(state.detail || "처리 경로를 확인하지 못했습니다.");
          action = state.action;
          if (action === "promote") {
            const preview = state.preview;
            if (!window.confirm("확정 DB에 반영할까요? 새 부서 " + preview.org_units_to_create + " / 새 업무 " + preview.duties_to_create + " / 새 연락처 " + preview.contacts_to_create + " / 검토 필요 행 " + preview.rows_requiring_review)) {
              button.disabled = false; return;
            }
          }
        } else if (action === "promote" && !window.confirm("표시된 미리보기의 안전한 항목을 확정 DB에 반영할까요?")) {
          button.disabled = false; return;
        }
        const suffix = agencyId ? "?agency_id=" + encodeURIComponent(agencyId) : "";
        const endpoint = action === "promote" ? "promote" : "detect";
        const response = await fetch("/api/extractions/" + extractionId + "/" + endpoint + suffix, {method: "POST", headers: csrfHeaders()});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "작업을 완료하지 못했습니다.");
        window.location.reload();
      } catch (error) {
        button.disabled = false;
        if (errorBox) { errorBox.textContent = error.message; errorBox.hidden = false; }
      }
    });
  });

  document.querySelectorAll("[data-coverage-source]").forEach((select) => {
    select.addEventListener("change", async () => {
      const previous = select.dataset.previous || select.defaultValue;
      const response = await fetch("/api/sources/" + select.dataset.coverageSource + "/coverage", {
        method: "PATCH", headers: csrfHeaders({"Content-Type": "application/json"}),
        body: JSON.stringify({coverage_mode: select.value}),
      });
      if (!response.ok) select.value = previous;
      else select.dataset.previous = select.value;
    });
    select.dataset.previous = select.value;
  });

  document.querySelector('[data-logout]')?.addEventListener('click', async () => {
    const response = await fetch('/logout', {method: 'POST', headers: csrfHeaders()});
    if (response.ok) window.location.href = '/login';
  });

  document.querySelector('[data-contact-export]')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    const form = document.querySelector('form[aria-label="연락처 검색"]');
    const query = form ? new URLSearchParams(new FormData(form)).toString() : '';
    button.disabled = true;
    try {
      const response = await fetch('/api/contacts/export?' + query, {method: 'POST', headers: csrfHeaders()});
      if (!response.ok) {
        const result = await response.json();
        throw new Error(result.detail || '엑셀 파일을 만들지 못했습니다.');
      }
      const blob = await response.blob();
      const disposition = response.headers.get('Content-Disposition') || '';
      const filename = disposition.match(/filename="?([^";]+)"?/)?.[1] || 'publicdb2_contacts.xlsx';
      const link = document.createElement('a');
      link.href = URL.createObjectURL(blob); link.download = filename; link.click();
      URL.revokeObjectURL(link.href);
    } catch (error) { window.alert(error.message); }
    finally { button.disabled = false; }
  });

  const userMutation = async (url, method, body) => {
    const response = await fetch(url, {method, headers: csrfHeaders({'Content-Type': 'application/json'}), body: JSON.stringify(body)});
    const result = await response.json();
    if (!response.ok) throw new Error(result.detail || '사용자 변경을 완료하지 못했습니다.');
    window.location.reload();
  };
  document.querySelectorAll('[data-user-active]').forEach((button) => button.addEventListener('click', async () => {
    try { await userMutation('/api/users/' + button.dataset.userActive + '/active', 'PATCH', {active: button.dataset.active === 'true'}); }
    catch (error) { window.alert(error.message); }
  }));
  document.querySelectorAll('[data-user-role]').forEach((button) => button.addEventListener('click', async () => {
    const role = window.prompt('새 역할을 입력하세요: ADMIN, OPERATOR, VIEWER');
    if (!role) return;
    try { await userMutation('/api/users/' + button.dataset.userRole + '/role', 'PATCH', {role: role.trim().toUpperCase()}); }
    catch (error) { window.alert(error.message); }
  }));
  document.querySelectorAll('[data-user-password]').forEach((button) => button.addEventListener('click', async () => {
    const password = window.prompt('새 비밀번호를 입력하세요. (12자 이상)');
    if (!password) return;
    try { await userMutation('/api/users/' + button.dataset.userPassword + '/password', 'POST', {password}); }
    catch (error) { window.alert(error.message); }
  }));
})();
