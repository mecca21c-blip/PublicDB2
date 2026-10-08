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

  const discoveryModal = document.querySelector('[data-modal="run-discovery"]');
  if (discoveryModal) {
    const status = discoveryModal.querySelector("[data-discovery-status]");
    const summary = discoveryModal.querySelector("[data-discovery-modal-summary]");
    const itemsRoot = discoveryModal.querySelector("[data-discovery-items]");
    const filters = discoveryModal.querySelector("[data-discovery-filters]");
    const resultsRegion = discoveryModal.querySelector("[data-discovery-results]");
    const pagination = discoveryModal.querySelector("[data-discovery-pagination]");
    const rangeState = discoveryModal.querySelector("[data-discovery-range-state]");
    const pageNumbers = discoveryModal.querySelector("[data-discovery-page-numbers]");
    let activeRun = null;
    let activePage = 1;
    let activeCategory = "ALL";
    let requestSequence = 0;
    let activeCategoryCounts = null;

    const renderSummary = (values) => {
      summary.replaceChildren();
      [
        ["유효 발견", values.semantic_discovery_count, ""],
        ["업무/명부", values.directory_records, ""],
        ["단독 연락처", values.standalone_contacts,
          "그중 사이트 공통 " + values.site_wide_contacts + " · 분류 미확인 " + values.unknown_contacts],
      ].forEach(([label, value, note]) => {
        const wrapper = document.createElement("div");
        const term = document.createElement("dt");
        const detail = document.createElement("dd");
        term.textContent = label;
        detail.textContent = String(value);
        if (note) {
          const subset = document.createElement("small");
          subset.textContent = note;
          detail.append(subset);
        }
        wrapper.append(term, detail);
        summary.append(wrapper);
      });
      summary.hidden = false;
    };

    const renderFilters = (counts) => {
      activeCategoryCounts = counts;
      filters.querySelectorAll("[data-discovery-category]").forEach((button) => {
        const count = counts[button.dataset.discoveryCategory] || 0;
        button.hidden = button.dataset.discoveryCategory !== "ALL" && count === 0;
        button.classList.toggle("is-active", button.dataset.discoveryCategory === activeCategory);
        button.setAttribute("aria-pressed", String(button.dataset.discoveryCategory === activeCategory));
        const label = button.dataset.baseLabel || button.textContent;
        button.dataset.baseLabel = label;
        button.textContent = label + " (" + count + ")";
      });
      filters.hidden = false;
    };

    const cell = (value, className = "") => {
      const node = document.createElement("td");
      node.textContent = value || "-";
      if (className) node.className = className;
      if (value) node.title = String(value);
      return node;
    };

    const evidenceCell = (item) => {
      const node = document.createElement("td");
      const details = document.createElement("details");
      const heading = document.createElement("summary");
      heading.className = "discovery-evidence-count";
      heading.textContent = item.supporting_evidence_count > 1
        ? "연락처 근거 " + item.supporting_evidence_count + "개"
        : "근거 보기";
      const content = document.createElement("div");
      content.className = "discovery-evidence-detail";
      [
        ["Observation", item.observation_id], ["Source URL", item.source_url],
        ["출처 위치", item.source_locator], ["업무", item.duty],
        ["전체 문맥", item.context], ["원문 요약", item.row_summary],
      ].forEach(([label, value]) => {
        if (!value) return;
        const line = document.createElement("p");
        const strong = document.createElement("strong");
        strong.textContent = label + ": ";
        line.append(strong, document.createTextNode(String(value)));
        content.append(line);
      });
      details.append(heading, content);
      node.append(details);
      return node;
    };

    const contactCell = (value) => {
      const node = document.createElement("td");
      const stack = document.createElement("span");
      stack.className = "discovery-contact-stack";
      String(value || "-").split(" · ").forEach((part) => {
        const line = document.createElement("span");
        line.textContent = part;
        stack.append(line);
      });
      node.append(stack);
      return node;
    };

    const resultTable = (items, kind) => {
      const group = document.createElement("section");
      group.className = "discovery-result-group";
      const title = document.createElement("h3");
      title.textContent = kind === "DIRECTORY" ? "업무/명부" : kind === "CONTACT" ? "단독 연락처" : "피드/API";
      const wrapper = document.createElement("div");
      wrapper.className = "table-scroll discovery-table-scroll";
      const table = document.createElement("table");
      table.className = "data-table discovery-table discovery-table--" + (kind === "DIRECTORY" ? "directory" : "contact");
      const head = document.createElement("thead");
      const headRow = document.createElement("tr");
      const labels = kind === "DIRECTORY"
        ? ["부서", "직위", "업무", "담당자", "연락처", "근거"]
        : ["종류", "값", "분류", "문맥", "출처", "근거"];
      labels.forEach((label) => {
        const th = document.createElement("th");
        th.scope = "col";
        th.textContent = label;
        headRow.append(th);
      });
      head.append(headRow);
      table.append(head);
      const body = document.createElement("tbody");
      items.forEach((item) => {
        const row = document.createElement("tr");
        if (kind === "DIRECTORY") {
          row.append(
            cell(item.org_unit), cell(item.position),
            cell(item.duty || item.row_summary, "discovery-text-preview"),
            cell(item.person_name), contactCell(item.contact_display), evidenceCell(item),
          );
        } else {
          row.append(
            cell(item.candidate_type_label || item.kind_label),
            cell(item.value || item.title || item.link, "url-cell"),
            cell(item.scope_label),
            cell(item.context || item.row_summary, "discovery-text-preview"),
            cell(item.source_locator, "url-cell"), evidenceCell(item),
          );
        }
        body.append(row);
      });
      table.append(body);
      wrapper.append(table);
      group.append(title, wrapper);
      return group;
    };

    const renderItems = (items) => {
      itemsRoot.replaceChildren();
      if (!items.length) {
        const empty = document.createElement("p");
        empty.className = "detail-note detail-note--empty";
        empty.textContent = "이 범주의 발견 데이터가 없습니다.";
        itemsRoot.append(empty);
        return;
      }
      ["DIRECTORY", "CONTACT", "FEED"].forEach((kind) => {
        const groupItems = items.filter((item) => item.kind === kind);
        if (groupItems.length) itemsRoot.append(resultTable(groupItems, kind));
      });
    };

    const renderPagination = (pageInfo) => {
      const first = pageInfo.total ? ((pageInfo.page - 1) * pageInfo.page_size) + 1 : 0;
      const last = Math.min(pageInfo.page * pageInfo.page_size, pageInfo.total);
      rangeState.textContent = first + "–" + last + " / " + pageInfo.total;
      pagination.querySelector('[data-discovery-page="previous"]').disabled = !pageInfo.has_previous;
      pagination.querySelector('[data-discovery-page="next"]').disabled = !pageInfo.has_next;
      pageNumbers.replaceChildren();
      const windowSize = 5;
      let firstPage = Math.max(1, pageInfo.page - Math.floor(windowSize / 2));
      firstPage = Math.min(firstPage, Math.max(1, pageInfo.pages - windowSize + 1));
      const lastPage = Math.min(pageInfo.pages, firstPage + windowSize - 1);
      for (let page = firstPage; page <= lastPage; page += 1) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button--secondary button--small discovery-page-number";
        button.textContent = String(page);
        button.dataset.discoveryPageNumber = String(page);
        if (page === pageInfo.page) button.setAttribute("aria-current", "page");
        button.addEventListener("click", () => loadDiscoveries(page));
        pageNumbers.append(button);
      }
      pagination.hidden = false;
    };

    const renderLoadState = (message, retry = false) => {
      status.replaceChildren();
      const text = document.createElement("span");
      text.textContent = message;
      status.append(text);
      if (retry) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "button button--secondary button--small";
        button.textContent = "다시 시도";
        button.addEventListener("click", () => loadDiscoveries(activePage));
        status.append(button);
      }
      status.hidden = false;
    };

    const loadDiscoveries = async (page) => {
      const sequence = ++requestSequence;
      resultsRegion.setAttribute("aria-busy", "true");
      renderLoadState("발견 데이터를 불러오는 중입니다.");
      itemsRoot.replaceChildren();
      rangeState.textContent = "불러오는 중";
      pageNumbers.replaceChildren();
      pagination.querySelector('[data-discovery-page="previous"]').disabled = true;
      pagination.querySelector('[data-discovery-page="next"]').disabled = true;
      try {
        const response = await fetch("/api/runs/" + encodeURIComponent(activeRun) + "/discoveries?page=" + page + "&page_size=15&category=" + encodeURIComponent(activeCategory));
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "발견 데이터를 불러오지 못했습니다.");
        if (sequence !== requestSequence) return;
        activePage = result.pagination.page;
        renderSummary(result.summary);
        renderFilters(result.category_counts);
        renderItems(result.items);
        status.hidden = true;
        renderPagination(result.pagination);
        resultsRegion.scrollTop = 0;
      } catch (_error) {
        if (sequence !== requestSequence) return;
        renderLoadState("발견 데이터를 불러오지 못했습니다.", true);
        rangeState.textContent = "— / —";
      } finally {
        if (sequence === requestSequence) resultsRegion.removeAttribute("aria-busy");
      }
    };

    document.querySelectorAll("[data-discovery-open]").forEach((button) => {
      button.addEventListener("click", () => {
        activeRun = button.dataset.runId;
        activePage = 1;
        activeCategory = "ALL";
        activeCategoryCounts = null;
        summary.hidden = true;
        filters.hidden = true;
        openModal(discoveryModal);
        loadDiscoveries(1);
      });
    });
    pagination.querySelectorAll("[data-discovery-page]").forEach((button) => {
      button.addEventListener("click", () => loadDiscoveries(activePage + (button.dataset.discoveryPage === "next" ? 1 : -1)));
    });
    filters.querySelectorAll("[data-discovery-category]").forEach((button) => {
      button.addEventListener("click", () => {
        activeCategory = button.dataset.discoveryCategory;
        activePage = 1;
        if (activeCategoryCounts) renderFilters(activeCategoryCounts);
        loadDiscoveries(1);
      });
    });
  }

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
      if (wizardSubmit && form.dataset.sourceCreate !== undefined) {
        wizardSubmit.disabled = apiActive && apiMode === "CATALOG" && !selectedCatalog;
      } else if (wizardSubmit && apiActive && apiMode === "CATALOG") {
        wizardSubmit.disabled = true;
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
    if (form.dataset.sourceCreate !== undefined) return;
    let step = 1;
    const selector = form.querySelector("[data-method-selector]");
    const scopeControls = [...form.querySelectorAll("[data-binding-scope]")];
    const orgField = form.querySelector("[data-org-scope-field]");
    const submit = form.querySelector("[data-wizard-submit]");
    const previewControls = form.querySelector("[data-scrape-preview-controls]");
    const previewResult = form.querySelector("[data-scrape-preview-result]");
    const syncConnection = () => {
      const agencySelected = Boolean(form.querySelector('[name="agency_id"]')?.value);
      const afterAgency = form.querySelector("[data-connection-after-agency]");
      if (afterAgency) afterAgency.hidden = !agencySelected;
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
        ? "여러 부서 페이지를 탐색하는 소스라면 기관 전체 연결을 권장합니다."
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
    form.querySelector('[name="agency_id"]')?.addEventListener("change", syncConnection);
    form.addEventListener("methodrefresh", () => { syncConnection(); invalidateScrapePreview(); });
    form.addEventListener("input", (event) => {
      if (!event.target.closest("[data-scrape-preview-result]")) invalidateScrapePreview();
    });
    form.addEventListener("change", (event) => {
      if (event.target.matches("[data-lookup-value]")) invalidateScrapePreview();
    });
    showStep(1);
  });

  document.querySelectorAll("[data-source-create]").forEach((form) => {
    const selector = form.querySelector("[data-method-selector]");
    const rowsRoot = form.querySelector("[data-source-grid-rows]");
    const rowTemplate = form.querySelector("[data-source-row-template]");
    const addButton = form.querySelector("[data-row-add]");
    const progress = form.querySelector("[data-row-progress]");
    const errorBox = form.querySelector("[data-form-error]");
    const resolution = form.querySelector("[data-row-resolution]");
    const submit = form.querySelector("[data-wizard-submit]");
    const queue = [];
    let activeDiscoveries = 0;
    let rowSequence = 0;
    let step = 1;
    let closed = false;
    let lastPreview = null;
    let registrationReady = false;
    let registering = false;

    const allRows = () => [...rowsRoot.querySelectorAll("[data-source-row]")];
    const stateOf = (row) => row._intakeState;
    const statusLabels = {
      WAITING: ["○", "입력 대기", "waiting"], QUEUED: ["◷", "확인 대기", "waiting"],
      DISCOVERING: ["◌", "확인 중", "running"], RESOLVED: ["✓", "연결 확인됨", "success"],
      PLANNED_NEW: ["+", "신규 등록 예정", "planned"], NEEDS_REVIEW: ["!", "확인 필요", "warning"],
      DUPLICATE: ["=", "중복 입력", "warning"], ERROR: ["×", "자동 확인 실패", "error"],
    };
    const setStatus = (row, code, detail = "") => {
      const output = row.querySelector("[data-row-status]");
      const [icon, label, tone] = statusLabels[code] || statusLabels.NEEDS_REVIEW;
      output.className = "row-status row-status--" + tone;
      output.replaceChildren();
      const mark = document.createElement("span"); mark.setAttribute("aria-hidden", "true"); mark.textContent = icon;
      output.append(mark, document.createTextNode(" " + label));
      output.title = detail || label;
      stateOf(row).status = code;
    };
    const updateProgress = () => {
      const rows = allRows().filter((row) => stateOf(row).url.trim());
      const done = rows.filter((row) => !["QUEUED", "DISCOVERING"].includes(stateOf(row).status)).length;
      const pending = rows.length - done;
      progress.textContent = pending ? "기관/부서 확인 중 " + done + " / " + rows.length : rows.length ? "확인 완료 " + done + " / " + rows.length : "입력 대기";
    };
    const normalizedClientUrl = (value) => {
      try {
        const parsed = new URL(value.trim());
        if (!["http:", "https:"].includes(parsed.protocol)) return null;
        parsed.hash = "";
        return parsed.href;
      } catch (_error) { return null; }
    };
    const updateDuplicates = () => {
      const seen = new Map();
      allRows().forEach((row) => {
        const state = stateOf(row);
        const normalized = normalizedClientUrl(state.url);
        if (!normalized) return;
        if (seen.has(normalized)) {
          state.duplicateOf = stateOf(seen.get(normalized)).id;
          setStatus(row, "DUPLICATE", "같은 URL이 위 행에 이미 있습니다.");
        } else {
          state.duplicateOf = null;
          seen.set(normalized, row);
        }
      });
      updateProgress();
    };
    const clearIdentity = (row) => {
      const state = stateOf(row);
      state.agencyName = ""; state.agencyId = null; state.agencyIntent = null;
      state.orgName = ""; state.orgId = null; state.orgIntent = null;
      row.querySelector("[data-row-agency]").value = "";
      row.querySelector("[data-row-agency-id]").value = "";
      row.querySelector("[data-row-org]").value = "";
      row.querySelector("[data-row-org-id]").value = "";
    };
    const discoveryContext = () => ({
      collection_method: selector.value,
      api_kind: form.querySelector("[data-api-kind]")?.value || null,
      auth_mode: form.querySelector("[data-api-auth]")?.value || null,
    });
    const runDiscoveryQueue = () => {
      while (!closed && activeDiscoveries < 2 && queue.length) {
        const job = queue.shift();
        const state = stateOf(job.row);
        if (job.generation !== state.generation || state.duplicateOf) continue;
        activeDiscoveries += 1;
        setStatus(job.row, "DISCOVERING"); updateProgress();
        fetch("/api/source-agency-discovery", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          signal: job.controller.signal,
          body: JSON.stringify({...discoveryContext(), representative_url: job.url}),
        }).then(async (response) => {
          const payload = await response.json();
          if (!response.ok) throw new Error(payload.detail || "기관을 자동 확인하지 못했습니다.");
          if (closed || job.generation !== state.generation || job.identityGeneration !== state.identityGeneration) return;
          state.discovery = payload;
          const agencyInput = job.row.querySelector("[data-row-agency]");
          const orgInput = job.row.querySelector("[data-row-org]");
          if (payload.existing_agency) {
            state.agencyName = payload.existing_agency.name;
            state.agencyId = payload.existing_agency.id;
            agencyInput.value = state.agencyName;
            job.row.querySelector("[data-row-agency-id]").value = state.agencyId;
          } else if (payload.candidate_name) {
            state.agencyName = payload.candidate_name;
            agencyInput.value = state.agencyName;
          }
          if (payload.existing_org_unit) {
            state.orgName = payload.existing_org_unit.name;
            state.orgId = payload.existing_org_unit.id;
            orgInput.value = state.orgName;
            job.row.querySelector("[data-row-org-id]").value = state.orgId;
          } else if (payload.org_unit_candidate) {
            state.orgName = payload.org_unit_candidate;
            orgInput.value = state.orgName;
          }
          const exact = Boolean(state.agencyId) && (!state.orgName || Boolean(state.orgId));
          setStatus(job.row, exact ? "RESOLVED" : "NEEDS_REVIEW", payload.message || "자동 확인 결과를 검토하세요.");
        }).catch((error) => {
          if (error.name === "AbortError" || closed || job.generation !== state.generation) return;
          setStatus(job.row, "ERROR", error.message);
        }).finally(() => {
          activeDiscoveries -= 1;
          state.controller = null;
          updateProgress();
          runDiscoveryQueue();
        });
      }
    };
    const enqueueDiscovery = (row) => {
      const state = stateOf(row);
      state.controller?.abort();
      state.generation += 1;
      const url = normalizedClientUrl(state.url);
      if (!url) {
        if (state.url.trim()) setStatus(row, "ERROR", "http 또는 https URL을 확인하세요.");
        else setStatus(row, "WAITING");
        updateProgress(); return;
      }
      updateDuplicates();
      if (state.duplicateOf) return;
      const controller = new AbortController();
      state.controller = controller;
      queue.push({row, url, generation: state.generation, identityGeneration: state.identityGeneration, controller});
      setStatus(row, "QUEUED"); updateProgress(); runDiscoveryQueue();
    };
    const lookup = (row, kind, input, results) => {
      const state = stateOf(row);
      window.clearTimeout(input._lookupTimer);
      const lookupGeneration = (input._lookupGeneration || 0) + 1;
      input._lookupGeneration = lookupGeneration;
      input._lookupTimer = window.setTimeout(async () => {
        const query = input.value.trim();
        if (!query || (kind === "org" && !state.agencyId)) { results.hidden = true; return; }
        const url = kind === "agency"
          ? "/api/lookups/agencies?q=" + encodeURIComponent(query) + "&limit=8"
          : "/api/lookups/agencies/" + encodeURIComponent(state.agencyId) + "/org-units?q=" + encodeURIComponent(query) + "&limit=8";
        const response = await fetch(url);
        if (!response.ok) { results.hidden = true; return; }
        const payload = await response.json();
        if (input._lookupGeneration !== lookupGeneration || input.value.trim() !== query) return;
        results.replaceChildren();
        payload.items.forEach((item) => {
          const button = document.createElement("button"); button.type = "button"; button.textContent = item.label;
          button.addEventListener("mousedown", (event) => {
            event.preventDefault(); input.value = item.name;
            if (kind === "agency") {
              state.agencyName = item.name; state.agencyId = item.id; state.agencyIntent = null;
              row.querySelector("[data-row-agency-id]").value = item.id;
              state.orgId = null; state.orgIntent = null; row.querySelector("[data-row-org-id]").value = "";
            } else {
              state.orgName = item.name; state.orgId = item.id; state.orgIntent = null;
              row.querySelector("[data-row-org-id]").value = item.id;
            }
            results.hidden = true; setStatus(row, "WAITING", "다음에서 서버 확인이 필요합니다.");
          });
          results.append(button);
        });
        results.hidden = !payload.items.length;
      }, 180);
    };
    const createRow = (url = "") => {
      const row = rowTemplate.content.firstElementChild.cloneNode(true);
      const state = {
        id: "row-" + (++rowSequence), url, agencyName: "", agencyId: null, agencyIntent: null,
        orgName: "", orgId: null, orgIntent: null, status: "WAITING", generation: 0,
        identityGeneration: 0, duplicateOf: null, controller: null, discovery: null,
      };
      row._intakeState = state;
      const urlInput = row.querySelector("[data-row-url]");
      const agencyInput = row.querySelector("[data-row-agency]");
      const orgInput = row.querySelector("[data-row-org]");
      urlInput.value = url;
      let urlTimer;
      urlInput.addEventListener("input", () => {
        const changed = state.url !== urlInput.value;
        state.url = urlInput.value;
        if (changed) {
          state.generation += 1; state.controller?.abort();
          clearIdentity(row); state.discovery = null;
        }
        window.clearTimeout(urlTimer);
        urlTimer = window.setTimeout(() => enqueueDiscovery(row), 450);
      });
      urlInput.addEventListener("paste", (event) => {
        if (selector.value !== "WEB_PAGE") return;
        const text = event.clipboardData?.getData("text") || "";
        const values = text.split(/\r?\n/).map((line) => line.split("\t")[0].trim()).filter(Boolean);
        if (values.length <= 1) return;
        event.preventDefault();
        const available = 200 - allRows().length + 1;
        if (values.length > available) {
          errorBox.textContent = "대화형 등록은 최대 200개입니다. 더 큰 목록은 엑셀 업로드를 사용하세요.";
          errorBox.hidden = false; return;
        }
        state.generation += 1; state.controller?.abort();
        state.url = values[0]; urlInput.value = values[0]; clearIdentity(row); enqueueDiscovery(row);
        values.slice(1).forEach((value) => { const added = createRow(value); enqueueDiscovery(added); });
      });
      agencyInput.addEventListener("input", () => {
        state.identityGeneration += 1; state.agencyName = agencyInput.value; state.agencyId = null; state.agencyIntent = null;
        row.querySelector("[data-row-agency-id]").value = "";
        state.orgId = null; state.orgIntent = null; row.querySelector("[data-row-org-id]").value = "";
        setStatus(row, "WAITING", "기관 입력을 서버에서 다시 확인합니다.");
        lookup(row, "agency", agencyInput, row.querySelector("[data-row-agency-results]"));
      });
      orgInput.addEventListener("input", () => {
        state.identityGeneration += 1; state.orgName = orgInput.value; state.orgId = null; state.orgIntent = null;
        row.querySelector("[data-row-org-id]").value = "";
        setStatus(row, "WAITING", "부서 입력을 서버에서 다시 확인합니다.");
        lookup(row, "org", orgInput, row.querySelector("[data-row-org-results]"));
      });
      row.querySelector("[data-row-remove]").addEventListener("click", () => {
        const duplicateRows = allRows().filter((candidate) => stateOf(candidate).status === "DUPLICATE");
        state.controller?.abort(); row.remove();
        if (!allRows().length) createRow();
        updateDuplicates();
        duplicateRows.filter((candidate) => candidate.isConnected && !stateOf(candidate).duplicateOf)
          .forEach((candidate) => enqueueDiscovery(candidate));
      });
      rowsRoot.append(row); setStatus(row, "WAITING"); updateProgress();
      return row;
    };
    const rowPayload = () => allRows().filter((row) => stateOf(row).url.trim()).map((row) => {
      const state = stateOf(row);
      return {
        row_id: state.id, url: state.url.trim(), agency_name: state.agencyName.trim(), agency_id: state.agencyId,
        agency_intent: state.agencyIntent, org_unit_name: state.orgName.trim(), org_unit_id: state.orgId,
        org_unit_intent: state.orgIntent,
      };
    });
    const batchPayload = () => {
      const values = jsonFromForm(form);
      let methodConfig = values.method_config || {};
      if (form.dataset.catalogConfig) {
        try { methodConfig = {...methodConfig, ...JSON.parse(form.dataset.catalogConfig)}; }
        catch (_error) { /* catalog remains server-validated */ }
      }
      return {
        collection_method: selector.value, rows: rowPayload(), description: values.description || null,
        method_config: methodConfig, scheduled_refresh_enabled: values.scheduled_refresh_enabled !== false,
      };
    };
    const applyPreview = (preview) => {
      const byId = new Map(allRows().map((row) => [stateOf(row).id, row]));
      preview.rows.forEach((item) => {
        const row = byId.get(item.row_id); if (!row) return;
        const state = stateOf(row);
        if (item.agency_id) { state.agencyId = item.agency_id; state.agencyName = item.agency_name; row.querySelector("[data-row-agency]").value = item.agency_name; }
        if (item.org_unit_id) { state.orgId = item.org_unit_id; state.orgName = item.org_unit_name; row.querySelector("[data-row-org]").value = item.org_unit_name; }
        setStatus(row, item.status, item.message);
      });
    };
    const summaryMarkup = (summary) => [
      ["수집 방식", {WEB_PAGE: "개별 URL · 스크래핑", WEB_CRAWL: "Index URL · 크롤링", API: "공개 API / RSS"}[selector.value]],
      ["입력 Source", summary.input], ["기관", summary.agency_count], ["부서 연결", summary.org_connections],
      ["기관 전체", summary.agency_wide_connections], ["신규 Source", summary.new_sources],
      ["기존 Source", summary.existing_sources], ["중복", summary.duplicates],
      ["새 연결", summary.new_bindings], ["기존 연결", summary.existing_bindings],
      ["신규 기관 예정", summary.new_agencies_planned], ["신규 부서 예정", summary.new_org_units_planned],
      ["확인 필요", summary.unresolved], ["오류", summary.errors],
    ].map(([label, value]) => "<span><strong>" + value + "</strong><small>" + label + "</small></span>").join("");
    const showStep = (next) => {
      step = next;
      if (step !== 3) registrationReady = false;
      form.querySelectorAll("[data-wizard-step]").forEach((section) => { section.hidden = Number(section.dataset.wizardStep) !== step; });
      form.querySelectorAll("[data-wizard-indicator]").forEach((item) => item.classList.toggle("is-active", Number(item.dataset.wizardIndicator) === step));
      form.querySelector("[data-wizard-prev]").hidden = step === 1;
      form.querySelector("[data-wizard-next]").hidden = step === 3;
      submit.hidden = step !== 3;
      submit.disabled = step !== 3 || !registrationReady || registering;
    };
    const renderFinal = (preview) => {
      form.querySelector("[data-final-summary]").innerHTML = summaryMarkup(preview.summary);
      const body = form.querySelector("[data-final-rows]"); body.replaceChildren();
      preview.rows.slice(0, 100).forEach((item) => {
        const tr = document.createElement("tr");
        [item.url, item.agency_name || "-", item.org_unit_name || "기관 전체", statusLabels[item.status]?.[1] || item.status].forEach((value) => {
          const td = document.createElement("td"); td.textContent = value; tr.append(td);
        });
        body.append(tr);
      });
      if (preview.rows.length > 100) {
        const tr = document.createElement("tr"); const td = document.createElement("td"); td.colSpan = 4;
        td.textContent = "나머지 " + (preview.rows.length - 100) + "개 행은 요약에 포함되어 있습니다."; tr.append(td); body.append(tr);
      }
    };
    const currentResolutionRows = (item) => item.row_ids.map((id) => allRows().find((row) => stateOf(row).id === id)).filter(Boolean);
    const resolveAgain = async (advanceToStepTwo = false) => {
      const response = await fetch("/api/source-bindings/interactive/preview", {
        method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}), body: JSON.stringify(batchPayload()),
      });
      const preview = await response.json();
      if (!response.ok) throw new Error(preview.detail || "등록 행을 확인하지 못했습니다.");
      lastPreview = preview; applyPreview(preview);
      if (preview.summary.errors) throw new Error("URL 오류가 있는 행을 수정하거나 삭제하세요.");
      const pending = [...preview.agency_resolutions, ...preview.org_resolutions];
      if (pending.length) { openResolution(pending[0], advanceToStepTwo); return false; }
      if (preview.summary.unresolved) throw new Error("기관 또는 부서를 확인해야 하는 행을 수정하세요.");
      if (!preview.summary.importable) throw new Error("등록할 수 있는 Source 행이 없습니다.");
      if (advanceToStepTwo) {
        form.querySelector("[data-step-two-summary]").innerHTML = summaryMarkup(preview.summary);
        showStep(2);
      }
      return true;
    };
    const openResolution = (item, advanceToStepTwo) => {
      resolution.hidden = false;
      resolution._item = item; resolution._advance = advanceToStepTwo;
      resolution.querySelector("[data-resolution-title]").textContent = item.kind === "AGENCY" ? "기관 확인" : "부서 확인";
      resolution.querySelector("[data-resolution-message]").textContent = item.kind === "AGENCY"
        ? '"' + item.name + '"은 등록되지 않은 기관입니다. ' + item.row_ids.length + "개 행에 한 번 적용합니다."
        : '"' + item.name + '"은 ' + item.agency_name + "에 등록되지 않은 부서입니다. " + item.row_ids.length + "개 행에 한 번 적용합니다.";
      const similar = resolution.querySelector("[data-resolution-similar]"); similar.replaceChildren();
      if (item.similar.length) {
        const label = document.createElement("p"); label.textContent = "비슷한 기존 항목"; similar.append(label);
        item.similar.forEach((candidate) => {
          const button = document.createElement("button"); button.type = "button"; button.className = "button button--secondary";
          button.textContent = candidate.name + " 사용";
          button.addEventListener("click", async () => {
            currentResolutionRows(item).forEach((row) => {
              const state = stateOf(row);
              if (item.kind === "AGENCY") {
                state.agencyName = candidate.name; state.agencyId = candidate.id; state.agencyIntent = null;
                row.querySelector("[data-row-agency]").value = candidate.name;
                state.orgId = null; state.orgIntent = null;
              } else {
                state.orgName = candidate.name; state.orgId = candidate.id; state.orgIntent = null;
                row.querySelector("[data-row-org]").value = candidate.name;
              }
            });
            resolution.hidden = true;
            try { await resolveAgain(advanceToStepTwo); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
          });
          similar.append(button);
        });
      }
      const agencyFields = resolution.querySelector("[data-resolution-agency-fields]");
      agencyFields.hidden = item.kind !== "AGENCY";
      resolution.querySelector("[data-resolution-clear-org]").hidden = item.kind !== "ORG_UNIT";
      if (item.kind === "AGENCY") {
        resolution.querySelector("[data-resolution-agency-name]").value = item.name;
        const firstRow = currentResolutionRows(item)[0];
        const firstDiscovery = firstRow ? stateOf(firstRow).discovery : null;
        resolution.querySelector("[data-resolution-agency-type]").value = firstDiscovery?.suggested_agency_type || "";
        resolution.querySelector("[data-resolution-agency-region]").value = firstDiscovery?.suggested_region_code || "";
      }
      resolution.querySelector("[data-resolution-error]").hidden = true;
    };
    resolution.querySelector("[data-resolution-new]").addEventListener("click", async () => {
      const item = resolution._item; const rows = currentResolutionRows(item);
      const resolutionError = resolution.querySelector("[data-resolution-error]");
      if (item.kind === "AGENCY") {
        const name = resolution.querySelector("[data-resolution-agency-name]").value.trim();
        const agencyType = resolution.querySelector("[data-resolution-agency-type]").value;
        if (!name || !agencyType) { resolutionError.textContent = "기관명과 기관 유형을 입력하세요."; resolutionError.hidden = false; return; }
        const intent = {
          official_name: name, agency_type: agencyType,
          region_code: resolution.querySelector("[data-resolution-agency-region]").value || null,
          external_identifier: resolution.querySelector("[data-resolution-agency-identifier]").value.trim() || null,
          address: resolution.querySelector("[data-resolution-agency-address]").value.trim() || null,
        };
        rows.forEach((row) => { const state = stateOf(row); state.agencyName = name; state.agencyId = null; state.agencyIntent = intent; row.querySelector("[data-row-agency]").value = name; });
      } else {
        rows.forEach((row) => { const state = stateOf(row); state.orgName = item.name; state.orgId = null; state.orgIntent = {name: item.name, unit_type: "DEPARTMENT"}; });
      }
      resolution.hidden = true;
      try { await resolveAgain(resolution._advance); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    });
    resolution.querySelector("[data-resolution-clear-org]").addEventListener("click", async () => {
      const item = resolution._item;
      currentResolutionRows(item).forEach((row) => { const state = stateOf(row); state.orgName = ""; state.orgId = null; state.orgIntent = null; row.querySelector("[data-row-org]").value = ""; });
      resolution.hidden = true;
      try { await resolveAgain(resolution._advance); } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    });
    resolution.querySelector("[data-resolution-edit]").addEventListener("click", () => { resolution.hidden = true; showStep(1); });
    form.querySelector("[data-wizard-next]").addEventListener("click", async () => {
      errorBox.hidden = true;
      try {
        if (step === 1) { await resolveAgain(true); return; }
        const panel = form.querySelector('[data-method-panel="' + selector.value + '"]');
        const invalid = panel?.querySelector(":invalid"); if (invalid) { invalid.reportValidity(); return; }
        const checks = [...(panel?.querySelectorAll('[name^="method_config.extract_"]') || [])];
        if (checks.length && !checks.some((control) => control.checked)) throw new Error("가져올 정보를 하나 이상 선택하세요.");
        const okay = await resolveAgain(false); if (!okay) return;
        registrationReady = lastPreview.summary.errors === 0
          && lastPreview.summary.unresolved === 0
          && lastPreview.summary.importable > 0;
        renderFinal(lastPreview); showStep(3);
      } catch (error) { errorBox.textContent = error.message; errorBox.hidden = false; }
    });
    form.querySelector("[data-wizard-prev]").addEventListener("click", () => {
      registrationReady = false;
      showStep(step - 1);
    });
    addButton.addEventListener("click", () => { if (allRows().length < 200 && selector.value === "WEB_PAGE") createRow().querySelector("[data-row-url]").focus(); });
    selector.addEventListener("change", () => {
      if (selector.value !== "WEB_PAGE") allRows().slice(1).forEach((row) => { stateOf(row).controller?.abort(); row.remove(); });
      addButton.hidden = selector.value !== "WEB_PAGE";
      form.querySelector("[data-crawl-row-help]").hidden = selector.value !== "WEB_CRAWL";
      const input = allRows()[0]?.querySelector("[data-row-url]");
      if (input) input.placeholder = selector.value === "WEB_CRAWL" ? "https://example.go.kr/departments/" : selector.value === "API" ? "https://api.example.go.kr/records" : "https://example.go.kr/page";
      allRows().forEach((row) => { stateOf(row).generation += 1; stateOf(row).controller?.abort(); if (stateOf(row).url.trim()) enqueueDiscovery(row); });
    });
    form.querySelectorAll("[data-api-kind-choice], [data-feed-kind], [data-api-auth]").forEach((control) => control.addEventListener("change", () => {
      allRows().forEach((row) => { if (stateOf(row).url.trim()) enqueueDiscovery(row); });
    }));
    form.addEventListener("catalogrow", (event) => {
      const row = allRows()[0]; if (!row || !event.detail?.endpoint) return;
      const state = stateOf(row); state.url = event.detail.endpoint;
      row.querySelector("[data-row-url]").value = state.url;
      clearIdentity(row); enqueueDiscovery(row);
    });
    form.querySelectorAll("[data-modal-close]").forEach((button) => button.addEventListener("click", () => {
      closed = true; queue.splice(0); allRows().forEach((row) => { stateOf(row).generation += 1; stateOf(row).controller?.abort(); });
    }));
    document.querySelector('[data-modal-open="source-create"]')?.addEventListener("click", () => { closed = false; });
    const showRegistrationResult = (result) => {
      const labels = [
        ["입력", result.summary.input], ["등록 처리", result.summary.registered], ["신규 Source", result.summary.created_sources],
        ["기존 Source 재사용", result.summary.existing_sources_reused], ["새 연결", result.summary.created_bindings],
        ["신규 기관", result.summary.created_agencies], ["신규 부서", result.summary.created_org_units],
        ["중복", result.summary.duplicates_skipped], ["오류", result.summary.errors],
      ];
      const output = form.querySelector("[data-interactive-result-summary]"); output.replaceChildren();
      labels.forEach(([label, value]) => { const dt = document.createElement("dt"); dt.textContent = label; const dd = document.createElement("dd"); dd.textContent = value + "개"; output.append(dt, dd); });
      const failedRows = result.rows.filter((item) => item.result === "ERROR" || (item.result === "SKIPPED" && !["DUPLICATE"].includes(item.status)));
      form.querySelector("[data-interactive-result-title]").textContent = failedRows.length ? "등록 완료 · 일부 오류" : "등록 완료";
      const errorSection = form.querySelector("[data-interactive-error-section]");
      const errorRows = form.querySelector("[data-interactive-error-rows]"); errorRows.replaceChildren();
      failedRows.forEach((item) => {
        const tr = document.createElement("tr");
        [item.url, statusLabels[item.status]?.[1] || item.result, item.message || "등록하지 못했습니다."]
          .forEach((value) => { const td = document.createElement("td"); td.textContent = value; tr.append(td); });
        errorRows.append(tr);
      });
      errorSection.hidden = failedRows.length === 0;
      form.querySelector("[data-interactive-fix]").hidden = failedRows.length === 0;
      form.dataset.createdSourceIds = JSON.stringify(result.source_ids || []);
      form.querySelector("[data-interactive-result]").hidden = false;
      form.querySelector(".source-review-table").hidden = true;
      form.querySelector(".modal-actions--sticky").hidden = true;
    };
    form.querySelector("[data-interactive-fix]").addEventListener("click", () => {
      form.querySelector("[data-interactive-result]").hidden = true;
      form.querySelector(".source-review-table").hidden = false;
      form.querySelector(".modal-actions--sticky").hidden = false;
      showStep(1);
    });
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      errorBox.textContent = "Source 등록은 3단계의 등록 버튼으로만 실행할 수 있습니다.";
      errorBox.hidden = false;
    });
    submit.addEventListener("click", async () => {
      if (registering) return;
      if (step !== 3 || !registrationReady) {
        errorBox.textContent = "등록 전 확인이 완료되지 않았습니다. 이전 단계의 오류와 확인 필요 항목을 확인하세요.";
        errorBox.hidden = false;
        return;
      }
      const originalLabel = submit.textContent;
      let requestSucceeded = false;
      registering = true; submit.disabled = true; submit.textContent = "등록 중...";
      errorBox.hidden = true;
      try {
        const response = await fetch("/api/source-bindings/interactive/register", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify(batchPayload()),
        });
        const text = await response.text();
        let result = {};
        try { result = text ? JSON.parse(text) : {}; }
        catch (_error) { throw new Error("등록 응답을 해석하지 못했습니다. 수집 소스 목록에서 결과를 확인하세요."); }
        if (!response.ok) throw new Error(result.detail || "행 기반 Source 등록을 완료하지 못했습니다.");
        requestSucceeded = true;
        showRegistrationResult(result);
        registrationReady = false;
      } catch (error) {
        errorBox.textContent = requestSucceeded
          ? "등록 요청은 성공했지만 결과 화면을 표시하지 못했습니다. 수집 소스 목록에서 등록 결과를 확인하세요."
          : error.message;
        errorBox.hidden = false;
        errorBox.scrollIntoView({block: "nearest"});
      } finally {
        registering = false; submit.textContent = originalLabel;
        submit.disabled = requestSucceeded || !registrationReady;
      }
    });
    createRow(); showStep(1);
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
      if (form.dataset.sourceCreate !== undefined) {
        form.dataset.catalogConfig = button.dataset.catalogConfig || "{}";
        form.dispatchEvent(new CustomEvent("catalogrow", {detail: {endpoint: button.dataset.catalogEndpoint || ""}}));
      }
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

  const settingsForm = document.querySelector("[data-settings-form]");
  if (settingsForm) {
    const enabled = settingsForm.querySelector("[data-auto-enabled]");
    const recurrence = settingsForm.querySelector("[data-refresh-recurrence]");
    const syncScheduleControls = () => {
      const active = Boolean(enabled?.checked);
      const mode = recurrence?.value || "WEEKLY";
      settingsForm.classList.toggle("is-muted", !active);
      settingsForm.setAttribute("data-auto-active", String(active));
      settingsForm.querySelectorAll("[data-schedule-field]").forEach((field) => {
        const visible = field.dataset.scheduleField === mode.toLowerCase();
        field.hidden = !visible;
        field.querySelectorAll("input, select").forEach((control) => { control.disabled = !visible; });
      });
    };
    enabled?.addEventListener("change", syncScheduleControls);
    recurrence?.addEventListener("change", syncScheduleControls);
    syncScheduleControls();

    const feedback = sessionStorage.getItem("publicdb2:settings-success");
    if (feedback) {
      const output = settingsForm.querySelector("[data-form-success]");
      if (output) { output.textContent = feedback; output.hidden = false; }
      sessionStorage.removeItem("publicdb2:settings-success");
    }
  }

  document.querySelectorAll("[data-api-form]").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      if (event.defaultPrevented) return;
      event.preventDefault();
      const errorBox = form.querySelector("[data-form-error]");
      try {
        const response = await fetch(form.action, {
          method: form.dataset.method || "POST",
          headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify(jsonFromForm(form)),
        });
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "요청을 처리하지 못했습니다.");
        if (form.dataset.successMessage) {
          sessionStorage.setItem("publicdb2:settings-success", form.dataset.successMessage);
        }
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
    const progress = job.completed_items + " / " + job.total_items;
    if (job.status === "PENDING") return "수집 대기 " + progress;
    if (job.status === "RUNNING") return "수집 중 " + progress;
    if (job.status === "COMPLETED") return "수집 완료 · " + progress;
    if (job.status === "COMPLETED_WITH_ERRORS") return "수집 완료 · 오류 " + job.failed_items + "건";
    if (job.status === "FAILED") return "수집 작업이 중단되었습니다.";
    if (job.status === "CANCELLED") return "수집 작업이 취소되었습니다.";
    return "수집 작업 상태 확인 중";
  };

  const currentItemText = (item) => {
    if (!item) return "";
    const context = [item.agency, item.org_unit].filter(Boolean).join(" · ");
    return [context, item.method, item.url].filter(Boolean).join(" · ");
  };

  const renderJobStatus = (job, output, accepted = false) => {
    if (!output) return;
    output.hidden = false;
    const title = output.querySelector?.("[data-job-status-title]");
    if (!title) { output.textContent = accepted ? "수집 작업을 시작했습니다. " + jobStatusText(job) : jobStatusText(job); return; }
    title.textContent = accepted ? "수집 작업을 시작했습니다." : jobStatusText(job);
    output.querySelector("[data-job-status-scope]").textContent = job.trigger_label || "수집 작업";
    output.querySelector("[data-job-status-progress]").textContent = jobStatusText(job);
    output.querySelector("[data-job-status-counts]").textContent = "성공 " + job.succeeded_items + " · 오류 " + job.failed_items;
    output.querySelector("[data-job-status-current]").textContent = currentItemText(job.current_item);
  };

  const pollCollectionJob = (job, output, onTerminal) => {
    renderJobStatus(job, output);
    const timer = window.setInterval(async () => {
      try {
        const response = await fetch("/api/collection-jobs/" + job.id + "?include_items=false");
        const result = await response.json();
        if (!response.ok) throw new Error(result.detail || "수집 작업 상태를 확인하지 못했습니다.");
        renderJobStatus(result.job, output);
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
    renderJobStatus(result.job, output, true);
    pollCollectionJob(result.job, output);
    return result.job;
  };

  const globalJobStatus = document.querySelector("[data-global-job-status]");
  const updateGlobalJobStatus = async () => {
    if (!globalJobStatus) return;
    try {
      const response = await fetch("/api/collection-jobs/active");
      if (!response.ok) { globalJobStatus.hidden = true; return; }
      const snapshot = await response.json();
      const job = snapshot.job;
      if (!job) {
        globalJobStatus.hidden = true;
        const sourcesPanel = document.querySelector(".collection-actions [data-job-status]");
        if (sourcesPanel) sourcesPanel.hidden = true;
        document.querySelectorAll("[data-source-live-status]").forEach((badge) => { badge.hidden = true; });
        return;
      }
      const terminal = ["COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"].includes(job.status);
      const label = snapshot.active_job_count > 1
        ? "수집 작업 " + snapshot.active_job_count + "개 진행 중/대기"
        : jobStatusText(job);
      globalJobStatus.querySelector("[data-global-job-text]").textContent = label;
      globalJobStatus.classList.toggle("is-terminal", terminal);
      globalJobStatus.hidden = false;

      const sourcesPanel = document.querySelector(".collection-actions [data-job-status]");
      if (sourcesPanel) renderJobStatus(job, sourcesPanel);
      document.querySelectorAll("[data-source-live-status]").forEach((badge) => { badge.hidden = true; });
      if (job.current_item) {
        const row = document.querySelector('[data-source-row="' + job.current_item.source_id + '"]');
        const badge = row?.querySelector("[data-source-live-status]");
        if (badge) {
          badge.textContent = job.current_item.status === "RUNNING" ? "수집 중" : "대기 중";
          badge.hidden = false;
        }
      }
    } catch (_error) {
      globalJobStatus.hidden = true;
    }
  };
  if (globalJobStatus) {
    updateGlobalJobStatus();
    window.setInterval(updateGlobalJobStatus, 3000);
  }

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
        button.textContent = "수집 요청됨";
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
        related.forEach((item) => { item.textContent = "수집 요청됨"; });
        renderJobStatus(result.job, errorBox, true);
        pollCollectionJob(result.job, errorBox);
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
    try {
      await createCollectionJob({trigger_type: "MANUAL_SELECTION", source_ids: sourceIds}, sharedJobStatus);
      event.currentTarget.textContent = "수집 요청됨";
    }
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
      event.currentTarget.textContent = "수집 요청됨";
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
      event.currentTarget.textContent = "수집 요청됨";
    } catch (error) {
      sharedJobStatus.hidden = false;
      sharedJobStatus.textContent = error.message;
      event.currentTarget.disabled = false;
    }
  });
  document.querySelectorAll("[data-job-agency]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    const output = button.closest(".detail-content")?.querySelector("[data-job-status]");
    try { await createCollectionJob({trigger_type: "MANUAL_AGENCY", agency_id: button.dataset.jobAgency}, output); button.textContent = "수집 요청됨"; }
    catch (error) { if (output) output.textContent = error.message; button.disabled = false; }
  }));
  document.querySelectorAll("[data-job-org-unit]").forEach((button) => button.addEventListener("click", async () => {
    button.disabled = true;
    const output = button.closest(".detail-content")?.querySelector("[data-job-status]");
    try { await createCollectionJob({trigger_type: "MANUAL_ORG_UNIT", org_unit_id: button.dataset.jobOrgUnit}, output); button.textContent = "수집 요청됨"; }
    catch (error) { if (output) output.textContent = error.message; button.disabled = false; }
  }));
  const liveJobRows = [...document.querySelectorAll("[data-job-summary]")];
  if (liveJobRows.some((row) => ["PENDING", "RUNNING"].includes(row.querySelector("[data-job-state]")?.dataset.jobStateCode))) {
    window.setInterval(async () => {
      await Promise.all(liveJobRows.map(async (row) => {
        const response = await fetch("/api/collection-jobs/" + row.dataset.jobSummary + "?include_items=false");
        if (!response.ok) return;
        const job = (await response.json()).job;
        row.querySelector("[data-job-progress]").textContent = job.completed_items + " / " + job.total_items + " (" + job.progress_percent + "%)";
        row.querySelector("[data-job-success]").textContent = job.succeeded_items;
        row.querySelector("[data-job-failed]").textContent = job.failed_items;
        const labels = {PENDING: "수집 대기", RUNNING: "수집 중", COMPLETED: "수집 완료", COMPLETED_WITH_ERRORS: "완료 · 오류 있음", FAILED: "작업 중단", CANCELLED: "취소됨"};
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

  const lookupSelect = (form, kind, item) => {
    const root = form.querySelector('[data-lookup-kind="' + kind + '"]');
    if (!root || !item?.id) return;
    const input = root.querySelector("[data-lookup-input]");
    const value = root.querySelector("[data-lookup-value]");
    input.value = item.label || item.name;
    value.value = item.id;
    if (kind === "org") value.dataset.agencyId = item.agency_id || form.querySelector('[name="agency_id"]')?.value || "";
    root.querySelector("[data-lookup-clear]").hidden = false;
    root.querySelector("[data-lookup-results]").hidden = true;
    input.setCustomValidity("");
    value.dispatchEvent(new Event("change", {bubbles: true}));
  };

  document.querySelectorAll("[data-agency-discover]").forEach((discoverButton) => {
    const form = discoverButton.closest("form");
    const method = form?.querySelector("[data-method-selector]");
    const result = form?.querySelector("[data-agency-discovery-result]");
    const representativeLabel = form?.querySelector("[data-discovery-representative]");
    const representativeSelect = form?.querySelector("[data-discovery-url]");
    const inlineAgency = form?.querySelector("[data-inline-agency-form]");
    const inlineOrg = form?.querySelector("[data-inline-org-form]");
    if (!form || !method || !result || !representativeLabel || !representativeSelect) return;
    let lastDiscovery = null;

    const validUrls = () => {
      const rawValues = method.value === "WEB_PAGE"
        ? (form.querySelector('[data-method-panel="WEB_PAGE"] textarea[name="urls"]')?.value || "").split(/\r?\n/)
        : [form.querySelector('[data-method-panel="' + method.value + '"] input[name="url"]')?.value || ""];
      const unique = new Map();
      rawValues.forEach((raw) => {
        try {
          const parsed = new URL(raw.trim());
          if (!["http:", "https:"].includes(parsed.protocol)) return;
          parsed.hash = "";
          unique.set(parsed.href, {url: parsed.href, host: parsed.hostname.toLowerCase()});
        } catch (_error) { /* incomplete input is handled by the wizard validation */ }
      });
      return [...unique.values()];
    };
    const representativeUrl = () => {
      const urls = validUrls();
      representativeLabel.hidden = true;
      if (!urls.length) return "";
      if (method.value !== "WEB_PAGE") return urls[0].url;
      const hosts = new Set(urls.map((item) => item.host));
      if (hosts.size <= 1) return urls[0].url;
      const previous = representativeSelect.value;
      representativeSelect.replaceChildren(new Option("대표 URL 선택", ""));
      urls.forEach((item) => representativeSelect.add(new Option(item.url, item.url)));
      if (urls.some((item) => item.url === previous)) representativeSelect.value = previous;
      representativeLabel.hidden = false;
      return representativeSelect.value;
    };
    const fingerprint = () => [
      method.value,
      form.querySelector("[data-api-kind]")?.value || "",
      form.querySelector("[data-api-auth]")?.value || "",
      representativeUrl(),
    ].join("|");
    const clearDiscovery = () => {
      if (!lastDiscovery && result.hidden) return;
      lastDiscovery = null;
      delete form.dataset.discoveryFingerprint;
      result.replaceChildren();
      result.hidden = true;
    };
    const message = (text, isError = false) => {
      result.replaceChildren();
      const paragraph = document.createElement("p");
      paragraph.textContent = text;
      if (isError) paragraph.setAttribute("role", "alert");
      result.append(paragraph);
      result.hidden = false;
    };
    const actionButton = (label, handler, primary = false) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "button " + (primary ? "button--primary" : "button--ghost");
      button.textContent = label;
      button.addEventListener("click", handler);
      return button;
    };
    const focusManualSearch = () => {
      form.querySelector('[data-lookup-kind="agency"] [data-lookup-input]')?.focus();
    };
    const openAgencyForm = (discovery = lastDiscovery) => {
      if (!inlineAgency) return;
      inlineAgency.hidden = false;
      const name = inlineAgency.querySelector("[data-inline-agency-name]");
      const type = inlineAgency.querySelector("[data-inline-agency-type]");
      const region = inlineAgency.querySelector("[data-inline-agency-region]");
      if (discovery?.candidate_name && !name.value) name.value = discovery.candidate_name;
      if (discovery?.suggested_agency_type && !type.value) type.value = discovery.suggested_agency_type;
      if (discovery?.suggested_region_code && !region.value) region.value = discovery.suggested_region_code;
      name.focus();
    };
    const renderDiscovery = (data) => {
      result.replaceChildren();
      const title = document.createElement("p");
      const evidenceCount = data.evidence_summary?.count || 0;
      title.textContent = data.existing_agency
        ? "등록된 기관과 정확히 일치합니다: " + data.existing_agency.name + " (근거 " + evidenceCount + "종)"
        : data.candidate_name
          ? "기관 후보: " + data.candidate_name + " (근거 " + evidenceCount + "종)"
          : (data.message || "기관 후보를 확인하지 못했습니다.");
      result.append(title);
      if (data.evidence?.length) {
        const evidence = document.createElement("ul");
        data.evidence.forEach((item) => {
          const row = document.createElement("li");
          row.textContent = item.label + ": " + item.snippet;
          evidence.append(row);
        });
        result.append(evidence);
      }
      const actions = document.createElement("div");
      actions.className = "inline-actions";
      if (data.existing_agency) {
        actions.append(actionButton("이 기관 사용", () => lookupSelect(form, "agency", data.existing_agency), true));
      } else {
        actions.append(actionButton("새 기관으로 등록", () => openAgencyForm(data), Boolean(data.candidate_name)));
      }
      actions.append(actionButton("직접 다른 기관 찾기", focusManualSearch));
      result.append(actions);
      result.hidden = false;
    };

    discoverButton.addEventListener("click", async () => {
      const url = representativeUrl();
      if (!url) {
        message(validUrls().length ? "서로 다른 호스트가 포함되어 있습니다. 확인할 대표 URL을 선택하세요." : "먼저 유효한 URL을 입력하세요.", true);
        return;
      }
      discoverButton.disabled = true;
      message("기관 정보를 확인하고 있습니다.");
      try {
        const response = await fetch("/api/source-agency-discovery", {
          method: "POST",
          headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify({
            collection_method: method.value,
            representative_url: url,
            api_kind: form.querySelector("[data-api-kind]")?.value || null,
            auth_mode: form.querySelector("[data-api-auth]")?.value || null,
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "기관 확인에 실패했습니다.");
        lastDiscovery = payload;
        form.dataset.discoveryFingerprint = fingerprint();
        renderDiscovery(payload);
      } catch (error) {
        lastDiscovery = null;
        message(error.message || "기관 확인에 실패했습니다.", true);
      } finally {
        discoverButton.disabled = false;
      }
    });

    representativeSelect.addEventListener("change", clearDiscovery);
    form.addEventListener("input", (event) => {
      if (!event.target.matches('textarea[name="urls"], input[name="url"]')) return;
      if (form.dataset.discoveryFingerprint && fingerprint() !== form.dataset.discoveryFingerprint) clearDiscovery();
    });
    form.addEventListener("change", (event) => {
      if (!event.target.matches("[data-method-selector], [data-api-kind-choice], [data-feed-kind], [data-api-auth], [data-discovery-url]")) return;
      if (form.dataset.discoveryFingerprint && fingerprint() !== form.dataset.discoveryFingerprint) clearDiscovery();
    });

    form.querySelector("[data-inline-agency-open]")?.addEventListener("click", () => openAgencyForm());
    form.querySelector("[data-inline-agency-cancel]")?.addEventListener("click", () => { inlineAgency.hidden = true; });
    form.querySelector("[data-inline-agency-save]")?.addEventListener("click", async (event) => {
      const errorBox = inlineAgency.querySelector("[data-inline-agency-error]");
      const name = inlineAgency.querySelector("[data-inline-agency-name]").value.trim();
      const agencyType = inlineAgency.querySelector("[data-inline-agency-type]").value;
      errorBox.hidden = true;
      if (!name || !agencyType) {
        errorBox.textContent = "기관명과 기관 유형을 입력하세요.";
        errorBox.hidden = false;
        return;
      }
      event.currentTarget.disabled = true;
      try {
        const response = await fetch("/api/agencies", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify({
            official_name: name,
            agency_type: agencyType,
            region_code: inlineAgency.querySelector("[data-inline-agency-region]").value || null,
            external_identifier: inlineAgency.querySelector("[data-inline-agency-identifier]").value.trim() || null,
            address: inlineAgency.querySelector("[data-inline-agency-address]").value.trim() || null,
          }),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "기관 등록에 실패했습니다.");
        lookupSelect(form, "agency", payload.item);
        inlineAgency.hidden = true;
      } catch (error) {
        errorBox.textContent = error.message || "기관 등록에 실패했습니다.";
        errorBox.hidden = false;
      } finally {
        event.currentTarget.disabled = false;
      }
    });

    form.querySelector("[data-inline-org-open]")?.addEventListener("click", () => {
      if (!form.querySelector('[name="agency_id"]')?.value) { focusManualSearch(); return; }
      inlineOrg.hidden = false;
      inlineOrg.querySelector("[data-inline-org-name]").focus();
    });
    form.querySelector("[data-inline-org-cancel]")?.addEventListener("click", () => { inlineOrg.hidden = true; });
    form.querySelector("[data-inline-org-save]")?.addEventListener("click", async (event) => {
      const agencyId = form.querySelector('[name="agency_id"]')?.value;
      const errorBox = inlineOrg.querySelector("[data-inline-org-error]");
      const name = inlineOrg.querySelector("[data-inline-org-name]").value.trim();
      errorBox.hidden = true;
      if (!agencyId || !name) {
        errorBox.textContent = agencyId ? "부서명을 입력하세요." : "기관을 먼저 선택하세요.";
        errorBox.hidden = false;
        return;
      }
      event.currentTarget.disabled = true;
      try {
        const response = await fetch("/api/agencies/" + encodeURIComponent(agencyId) + "/org-units", {
          method: "POST", headers: csrfHeaders({"Content-Type": "application/json"}),
          body: JSON.stringify({name, unit_type: "DEPARTMENT", parent_org_unit_id: null}),
        });
        const payload = await response.json();
        if (!response.ok) throw new Error(payload.detail || "부서 등록에 실패했습니다.");
        lookupSelect(form, "org", payload.item);
        inlineOrg.hidden = true;
      } catch (error) {
        errorBox.textContent = error.message || "부서 등록에 실패했습니다.";
        errorBox.hidden = false;
      } finally {
        event.currentTarget.disabled = false;
      }
    });
    form.querySelector('[name="agency_id"]')?.addEventListener("change", () => {
      if (inlineOrg) inlineOrg.hidden = true;
    });
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
