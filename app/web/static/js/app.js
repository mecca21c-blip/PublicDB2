(() => {
  "use strict";
  document.documentElement.classList.add("js-ready");

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

  document.querySelectorAll("[data-modal-open]").forEach((button) => {
    button.addEventListener("click", () => {
      const modal = document.querySelector('[data-modal="' + button.dataset.modalOpen + '"]');
      if (!modal) return;
      modal.hidden = false;
      document.body.style.overflow = "hidden";
      modal.querySelector("[data-modal-close]")?.focus();
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

  const jsonFromForm = (form) => {
    const payload = {};
    new FormData(form).forEach((value, key) => {
      const cleaned = typeof value === "string" ? value.trim() : value;
      payload[key] = cleaned === "" ? null : cleaned;
    });
    return payload;
  };

  document.querySelectorAll("[data-api-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const errorBox = form.querySelector("[data-form-error]");
      try {
        const response = await fetch(form.action, {
          method: form.dataset.method || "POST",
          headers: {"Content-Type": "application/json"},
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
        headers: {"Content-Type": "application/json"},
        body: button.dataset.body || "{}",
      });
      if (response.ok) window.location.reload();
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
        const response = await fetch("/api/sources/" + sourceId + "/collect", {method: "POST"});
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "수집을 완료하지 못했습니다.");
        window.location.reload();
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

  document.querySelectorAll("[data-agency-select]").forEach((agencySelect) => {
    const orgSelect = agencySelect.form?.querySelector('[name="org_unit_id"]');
    if (!orgSelect) return;
    const filterUnits = () => {
      orgSelect.querySelectorAll("[data-agency-id]").forEach((option) => {
        option.hidden = option.dataset.agencyId !== agencySelect.value;
      });
      if (orgSelect.selectedOptions[0]?.hidden) orgSelect.value = "";
    };
    agencySelect.addEventListener("change", filterUnits);
    filterUnits();
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
      fetch("/api/source-bindings/imports/" + previewToken, {method: "DELETE", keepalive: true});
      previewToken = null;
    };
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      discardPreview();
      errorBox.hidden = true;
      confirmButton.disabled = true;
      setStep("columns");
      try {
        const response = await fetch(form.action, {method: "POST", body: new FormData(form)});
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
        const response = await fetch("/api/source-bindings/imports/" + previewToken + "/confirm", {method: "POST"});
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
          method: "POST", headers: {"Content-Type": "application/json"}, body: "{}",
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
        const response = await fetch("/api/extractions/" + extractionId + "/" + endpoint + suffix, {method: "POST"});
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
        method: "PATCH", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({coverage_mode: select.value}),
      });
      if (!response.ok) select.value = previous;
      else select.dataset.previous = select.value;
    });
    select.dataset.previous = select.value;
  });
})();
