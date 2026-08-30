(() => {
  "use strict";

  window.name = "tag-manager";

  const $ = (id) => document.getElementById(id);
  const CANDIDATE_STORAGE_KEY = "offline-file-namer-tag-manager-candidates-v1";
  const WORKFLOW_TAG_UPDATE_KEY = "offline-file-namer-workflow-tags-updated-v1";
  const scopeLabels = { workflow: "工作流级", group: "组级", record: "文件级", suffix: "后缀" };
  const state = {
    workflows: [],
    workflow: null,
    tags: {},
    candidates: [],
    fieldId: "",
    selectedTagId: "",
    search: "",
    status: "all",
    editingId: "",
    pendingCandidate: null,
    deletingTag: null,
    workbookName: "",
    workbookExists: false,
  };
  let toastTimer;

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const tagValue = (tag) => typeof tag === "string" ? tag : String(tag?.value ?? "");
  const tagLabel = (tag) => typeof tag === "string" ? tag : String(tag?.label ?? tag?.value ?? "");
  const query = new URLSearchParams(window.location.search);

  function startClientLifecycle() {
    const heartbeat = () => {
      fetch("/api/client-heartbeat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
        cache: "no-store",
        keepalive: true,
      }).catch(() => {});
    };
    heartbeat();
    window.setInterval(heartbeat, 2000);
    window.addEventListener("pagehide", (event) => {
      if (event.persisted) return;
      const body = new Blob(["{}"], { type: "application/json" });
      if (!navigator.sendBeacon || !navigator.sendBeacon("/api/client-closed", body)) {
        fetch("/api/client-closed", { method: "POST", body: "{}", keepalive: true }).catch(() => {});
      }
    });
  }

  function notifyWorkflowTagUpdate() {
    try {
      localStorage.setItem(WORKFLOW_TAG_UPDATE_KEY, JSON.stringify({
        workflow_id: state.workflow?.id || "",
        changed_at: Date.now(),
      }));
    } catch (_error) {
      // The XLSX write is authoritative even if browser storage is unavailable.
    }
  }

  function startWorkflowTagSync() {
    window.addEventListener("storage", (event) => {
      if (event.key !== WORKFLOW_TAG_UPDATE_KEY || !event.newValue) return;
      let update;
      try { update = JSON.parse(event.newValue); } catch (_error) { return; }
      if (String(update?.workflow_id || "") !== String(state.workflow?.id || "")) return;
      loadValues(state.workflow.id)
        .then((tags) => { state.tags = tags; render(); })
        .catch(() => {});
    });
  }

  function showToast(message, error = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.className = "toast"; }, 3200);
  }

  function applyTheme(theme) {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    $("themeIcon").textContent = dark ? "☀" : "☾";
    $("themeLabel").textContent = dark ? "浅色" : "深色";
    localStorage.setItem("offline-file-namer-theme", dark ? "dark" : "light");
  }

  function normaliseTag(tag, index, source = "工作流默认") {
    const raw = typeof tag === "string" ? { label: tag, value: tag } : (tag || {});
    return {
      id: String(raw.id || `tag-${index + 1}-${String(raw.value || raw.label || "value").toLowerCase().replace(/[^a-z0-9]+/g, "-")}`),
      label: String(raw.label ?? raw.value ?? ""),
      value: String(raw.value ?? raw.label ?? ""),
      aliases: Array.isArray(raw.aliases) ? raw.aliases.map(String) : String(raw.aliases || "").split(/[，,、]/).map((item) => item.trim()).filter(Boolean),
      enabled: raw.enabled !== false,
      usage: Number(raw.usage || 0),
      source: String(raw.source || source),
    };
  }

  async function loadValues(workflowId) {
    const response = await fetch(`/api/workflow-values?workflow_id=${encodeURIComponent(workflowId)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || payload.ok === false || !payload.data) throw new Error(payload.error || "工作流值加载失败");
    state.workbookName = payload.data.workbook_name || "workflow-values.xlsx";
    state.workbookExists = !!payload.data.exists;
    return payload.data.tags || {};
  }

  function fields() { return state.workflow?.fields || []; }
  function fieldById(fieldId) { return fields().find((field) => field.id === fieldId); }
  function currentField() { return fieldById(state.fieldId) || fields()[0]; }
  function fieldTags(fieldId = state.fieldId) { return state.tags[fieldId] || []; }

  function candidateId(fieldId, value) {
    let hash = 2166136261;
    for (const character of fieldId + "\u0000" + value) {
      hash ^= character.codePointAt(0);
      hash = Math.imul(hash, 16777619);
    }
    return "candidate-" + Math.abs(hash);
  }

  function savedCandidates(workflowId) {
    try {
      const saved = JSON.parse(localStorage.getItem(CANDIDATE_STORAGE_KEY) || "{}");
      const list = saved.candidates?.[workflowId];
      return Array.isArray(list) ? list : [];
    } catch (_error) {
      return [];
    }
  }

  async function loadCandidates(workflow) {
    const saved = savedCandidates(workflow.id);
    const savedByValue = new Map(saved.map((candidate) => [candidate.fieldId + "\u0000" + candidate.value, candidate]));
    const aggregate = new Map();
    try {
      const response = await fetch("/api/state", { cache: "no-store" });
      const payload = await response.json();
      const current = payload.ok ? payload.state : null;
      if (String(current?.workflow?.active_id || "") !== String(workflow.id)) return [];
      for (const record of current.records || []) {
        for (const [fieldId, values] of Object.entries(record.workflow_candidates || {})) {
          for (const rawValue of Array.isArray(values) ? values : []) {
            const value = String(rawValue || "").trim();
            if (!value) continue;
            const key = fieldId + "\u0000" + value;
            const item = aggregate.get(key) || {
              id: candidateId(fieldId, value), fieldId, value, count: 0,
              examples: [], status: "pending", reason: "",
            };
            item.count += 1;
            const example = [record.relative_folder, record.original_name].filter(Boolean).join("/") || record.path || "";
            if (example && !item.examples.includes(example) && item.examples.length < 3) item.examples.push(example);
            const details = record.workflow_candidate_details?.[fieldId] || [];
            const detail = details.find((entry) => String(entry?.value || "") === value);
            if (detail?.reason && !item.reason) item.reason = detail.reason;
            aggregate.set(key, item);
          }
        }
      }
    } catch (_error) {
      return [];
    }
    return [...aggregate.values()].map((candidate) => {
      const savedCandidate = savedByValue.get(candidate.fieldId + "\u0000" + candidate.value);
      return {
        ...candidate,
        examples: candidate.examples.join("、"),
        status: savedCandidate?.status === "approved" || savedCandidate?.status === "ignored" ? savedCandidate.status : "pending",
      };
    });
  }

  function saveCandidates() {
    let saved = {};
    try { saved = JSON.parse(localStorage.getItem(CANDIDATE_STORAGE_KEY) || "{}"); } catch (_error) { saved = {}; }
    saved.candidates = saved.candidates || {};
    saved.candidates[state.workflow.id] = state.candidates;
    localStorage.setItem(CANDIDATE_STORAGE_KEY, JSON.stringify(saved));
  }

  function render() {
    const field = currentField();
    if (!field) return;
    state.fieldId = field.id;
    $("workflowDescription").textContent = state.workflow.description || "这个工作流还没有填写说明。";
    $("fieldCount").textContent = String(fields().length);
    $("metricField").textContent = field.label;
    const allTags = Object.values(state.tags).flat();
    $("metricTags").textContent = String(allTags.length);
    $("metricEnabled").textContent = String(allTags.filter((tag) => tag.enabled).length);
    $("metricCandidates").textContent = String(state.candidates.filter((candidate) => candidate.status === "pending").length);
    $("saveNote").textContent = state.workbookExists ? `XLSX：${state.workbookName}` : "尚未创建 XLSX，首次保存时生成";
    renderWorkflowSelect();
    renderFields();
    renderTags(field);
    renderCandidates();
    renderInspector();
  }

  function renderWorkflowSelect() {
    $("workflowSelect").innerHTML = state.workflows.map((workflow) => `<option value="${esc(workflow.id)}" ${workflow.id === state.workflow.id ? "selected" : ""}>${esc(workflow.name)}</option>`).join("");
  }

  function renderFields() {
    $("fieldList").innerHTML = fields().map((field) => {
      const count = fieldTags(field.id).length;
      const active = field.id === state.fieldId ? "active" : "";
      return `<button type="button" class="${active}" data-field-id="${esc(field.id)}"><span><span class="field-name">${esc(field.label)}</span><span class="field-scope">${esc(scopeLabels[field.scope] || field.scope)} · ${field.kind === "choice" ? "选项" : "文本"}</span></span><span class="field-count">${count}</span></button>`;
    }).join("");
  }

  function renderTags(field) {
    $("fieldTitle").textContent = field.label;
    $("fieldMeta").textContent = `${scopeLabels[field.scope] || field.scope} · ${fieldTags(field.id).length} 个快捷值 · 字段 ID：${field.id}`;
    const search = state.search.trim().toLowerCase();
    const visible = fieldTags(field.id).filter((tag) => {
      const text = [tag.label, tag.value, ...(tag.aliases || [])].join(" ").toLowerCase();
      return (!search || text.includes(search)) && (state.status === "all" || (state.status === "enabled" ? tag.enabled : !tag.enabled));
    });
    if (!visible.length) {
      $("tagRows").innerHTML = `<tr><td colspan="8"><div class="manager-empty"><div><strong>${search || state.status !== "all" ? "没有符合条件的标签" : "还没有快捷标签"}</strong><span>点击“新增标签”开始维护这个位置。</span></div></div></td></tr>`;
      return;
    }
    $("tagRows").innerHTML = visible.map((tag, index) => `<tr data-tag-id="${esc(tag.id)}">
      <td class="manager-index-col">${index + 1}</td>
      <td><span class="tag-title">${esc(tag.label)}</span><span class="tag-subline">${esc(tag.id)}</span></td>
      <td><code>${esc(tag.value)}</code></td>
      <td><span class="tag-aliases">${tag.aliases?.length ? esc(tag.aliases.join("、")) : "-"}</span></td>
      <td><span class="tag-status${tag.enabled ? "" : " disabled"}">${tag.enabled ? "已启用" : "已停用"}</span></td>
      <td>${esc(tag.usage)}</td>
      <td><span class="tag-source">${esc(tag.source)}</span></td>
      <td><button class="table-action" type="button" data-action="edit">编辑</button><button class="table-action" type="button" data-action="toggle">${tag.enabled ? "停用" : "启用"}</button><button class="table-action danger" type="button" data-action="delete">删除</button></td>
    </tr>`).join("");
  }

  function renderCandidates() {
    const activeCandidates = state.candidates.filter((candidate) => candidate.fieldId === state.fieldId);
    if (!activeCandidates.length) {
      $("candidateList").innerHTML = `<div class="candidate-empty">当前标签位置没有待确认候选。解析时发现的新内容会出现在这里。</div>`;
      return;
    }
    $("candidateList").innerHTML = activeCandidates.map((candidate) => {
      const field = fieldById(candidate.fieldId);
      const done = candidate.status !== "pending";
      return `<div class="candidate-row ${done ? candidate.status : ""}" data-candidate-id="${esc(candidate.id)}">
        <span class="candidate-value">${esc(candidate.value)}</span>
        <span class="candidate-field">${esc(field?.label || candidate.fieldId)}</span>
        <span class="candidate-count">${esc(candidate.count)} 次</span>
        <span class="candidate-examples" title="${esc(candidate.examples)}">${esc(candidate.examples)}</span>
        <span class="candidate-actions">${done ? `<span class="candidate-field">${candidate.status === "approved" ? "已添加" : "已忽略"}</span>` : `<button data-candidate-action="add">添加标签</button><button data-candidate-action="ignore">忽略</button>`}</span>
      </div>`;
    }).join("");
  }

  function previewFor(tag) {
    const values = {};
    for (const field of fields()) values[field.id] = fieldTags(field.id).find((item) => item.enabled)?.value || `<${field.label}>`;
    if (tag) values[state.fieldId] = tag.value;
    const template = state.workflow.template || [];
    const parts = template.map((part) => part.literal !== undefined ? part.literal : values[part.field] || "").filter(Boolean);
    return (parts.join("").replace(/_{2,}/g, "_") || "示例文件") + ".wav";
  }

  function renderInspector() {
    const tag = fieldTags().find((item) => item.id === state.selectedTagId);
    if (!tag) {
      $("tagInspector").innerHTML = "选择一个标签查看详情";
      return;
    }
    const field = currentField();
    $("tagInspector").innerHTML = `<div class="inspector-name">${esc(tag.label)}</div>
      <div class="inspector-value">${esc(tag.value)}</div><div class="inspector-rule"></div>
      <dl class="inspector-list"><dt>位置</dt><dd>${esc(field.label)}</dd><dt>别名</dt><dd>${tag.aliases?.length ? esc(tag.aliases.join("、")) : "未设置"}</dd><dt>状态</dt><dd>${tag.enabled ? "已启用" : "已停用"}</dd><dt>来源</dt><dd>${esc(tag.source)}</dd><dt>使用</dt><dd>${esc(tag.usage)} 次</dd></dl>
      <div class="inspector-preview">${esc(previewFor(tag))}</div>`;
  }

  function openModal(tag = null, candidate = null) {
    state.editingId = tag?.id || "";
    state.pendingCandidate = candidate;
    $("modalTitle").textContent = tag ? "编辑标签" : "新增标签";
    $("tagField").innerHTML = fields().map((field) => `<option value="${esc(field.id)}" ${(tag?.fieldId || candidate?.fieldId || state.fieldId) === field.id ? "selected" : ""}>${esc(field.label)} · ${esc(scopeLabels[field.scope] || field.scope)}</option>`).join("");
    $("tagLabel").value = tag?.label || candidate?.value || "";
    $("tagValue").value = tag?.value || candidate?.value || "";
    $("tagAliases").value = tag?.aliases?.join("、") || "";
    $("tagEnabled").checked = tag ? tag.enabled : true;
    $("modalHint").textContent = candidate ? `来自解析候选：出现 ${candidate.count} 次。确认后会加入当前工作流的标签库。` : "实际值和显示名称可以不同；分隔符由工作流统一处理。";
    $("tagModal").classList.remove("hidden");
    window.setTimeout(() => $("tagLabel").focus(), 0);
  }

  function closeModal() {
    $("tagModal").classList.add("hidden");
    state.editingId = "";
    state.pendingCandidate = null;
  }

  async function saveTag(event) {
    event.preventDefault();
    const fieldId = $("tagField").value;
    const label = $("tagLabel").value.trim();
    const value = $("tagValue").value.trim();
    const aliases = $("tagAliases").value.split(/[，,、]/).map((item) => item.trim()).filter(Boolean);
    if (!label || !value) return showToast("显示名称和实际值不能为空", true);
    const list = state.tags[fieldId] || (state.tags[fieldId] = []);
    const duplicate = list.find((tag) => tag.id !== state.editingId && tag.value.toLowerCase() === value.toLowerCase());
    if (duplicate) return showToast(`实际值已存在：${duplicate.label}`, true);
    const existing = list.find((tag) => tag.id === state.editingId);
    const item = { id: state.editingId, label, value, aliases, enabled: $("tagEnabled").checked, default: existing?.default || false, order: existing?.order || list.length + 1, notes: existing?.notes || "", source: state.pendingCandidate ? "解析确认" : (state.editingId ? (existing?.source || "用户编辑") : "用户新增") };
    try {
      const response = await fetch("/api/workflow-values/tag", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ workflow_id: state.workflow.id, field_id: fieldId, tag: item }) });
      const payload = await response.json();
      if (!response.ok || payload.ok === false || !payload.data) throw new Error(payload.error || "标签保存失败");
      state.tags = payload.data.tags || state.tags;
      state.workbookName = payload.data.workbook_name || state.workbookName;
      state.workbookExists = !!payload.data.exists;
      notifyWorkflowTagUpdate();
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    state.fieldId = fieldId;
    state.selectedTagId = (state.tags[fieldId] || []).find((tag) => tag.value === value)?.id || item.id;
    if (state.pendingCandidate) {
      const candidate = state.candidates.find((entry) => entry.id === state.pendingCandidate.id);
      if (candidate) candidate.status = "approved";
      saveCandidates();
    }
    closeModal();
    render();
    showToast(`已保存“${label}”`);
  }

  async function toggleTag(tag) {
    try {
      const response = await fetch("/api/workflow-values/tag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow_id: state.workflow.id, field_id: state.fieldId, action: "toggle", tag_id: tag.id }),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false || !payload.data) throw new Error(payload.error || "标签状态保存失败");
      state.tags = payload.data.tags || state.tags;
      state.workbookName = payload.data.workbook_name || state.workbookName;
      state.workbookExists = !!payload.data.exists;
      notifyWorkflowTagUpdate();
      render();
      const updated = fieldTags().find((item) => item.id === tag.id);
      showToast(updated?.enabled ? `已启用“${tag.label}”` : `已停用“${tag.label}”`);
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function openDeleteModal(tag) {
    state.deletingTag = { ...tag, fieldId: state.fieldId };
    $("deleteTagField").textContent = currentField()?.label || state.fieldId;
    $("deleteTagName").textContent = tag.label;
    $("deleteTagModal").classList.remove("hidden");
    window.setTimeout(() => $("confirmDeleteTag").focus(), 0);
  }

  function closeDeleteModal() {
    $("deleteTagModal").classList.add("hidden");
    state.deletingTag = null;
  }

  async function deleteTag() {
    const tag = state.deletingTag;
    if (!tag) return;
    const confirmButton = $("confirmDeleteTag");
    confirmButton.disabled = true;
    try {
      const response = await fetch("/api/workflow-values/tag", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ workflow_id: state.workflow.id, field_id: tag.fieldId, action: "delete", tag_id: tag.id }),
      });
      const payload = await response.json();
      if (!response.ok || payload.ok === false || !payload.data) throw new Error(payload.error || "标签删除失败");
      state.tags = payload.data.tags || state.tags;
      state.workbookName = payload.data.workbook_name || state.workbookName;
      state.workbookExists = !!payload.data.exists;
      if (state.selectedTagId === tag.id) state.selectedTagId = "";
      const candidate = state.candidates.find((item) => item.fieldId === tag.fieldId && item.value === tag.value && item.status === "approved");
      if (candidate) {
        candidate.status = "pending";
        saveCandidates();
      }
      notifyWorkflowTagUpdate();
      closeDeleteModal();
      render();
      showToast(`已删除“${tag.label}”`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      confirmButton.disabled = false;
    }
  }

  async function selectWorkflow(workflowId) {
    const response = await fetch(`/api/workflows?workflow_id=${encodeURIComponent(workflowId)}`, { cache: "no-store" });
    const payload = await response.json();
    if (!response.ok || payload.ok === false) throw new Error(payload.error || "工作流加载失败");
    const workflow = payload.active;
    if (!workflow) throw new Error("工作流不存在");
    state.workflow = workflow;
    state.tags = await loadValues(workflow.id);
    state.candidates = await loadCandidates(workflow);
    state.fieldId = fieldById(query.get("field"))?.id || fields()[0]?.id || "";
    state.selectedTagId = "";
    render();
  }

  async function refreshValues() {
    try {
      state.tags = await loadValues(state.workflow.id);
      state.candidates = await loadCandidates(state.workflow);
      state.selectedTagId = "";
      render();
      showToast("已从 XLSX 重新加载");
    } catch (error) { showToast(error.message, true); }
  }

  function bindEvents() {
    applyTheme(localStorage.getItem("offline-file-namer-theme") || "light");
    startWorkflowTagSync();
    $("themeToggle").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
    $("workflowSelect").addEventListener("change", (event) => selectWorkflow(event.target.value).catch((error) => showToast(error.message, true)));
    $("fieldList").addEventListener("click", (event) => {
      const button = event.target.closest("[data-field-id]");
      if (!button) return;
      state.fieldId = button.dataset.fieldId;
      state.selectedTagId = "";
      state.search = "";
      $("tagSearch").value = "";
      render();
    });
    $("tagSearch").addEventListener("input", (event) => { state.search = event.target.value; render(); });
    $("tagStatus").addEventListener("change", (event) => { state.status = event.target.value; render(); });
    $("addTagButton").addEventListener("click", () => openModal());
    $("refreshValues").addEventListener("click", refreshValues);
    $("tagRows").addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-tag-id]");
      if (!row) return;
      const tag = fieldTags().find((item) => item.id === row.dataset.tagId);
      if (!tag) return;
      if (event.target.dataset.action === "edit") openModal(tag);
      else if (event.target.dataset.action === "toggle") toggleTag(tag);
      else if (event.target.dataset.action === "delete") openDeleteModal(tag);
      else { state.selectedTagId = tag.id; render(); }
    });
    $("candidateList").addEventListener("click", (event) => {
      const row = event.target.closest("[data-candidate-id]");
      const candidate = state.candidates.find((item) => item.id === row?.dataset.candidateId);
      if (!candidate || candidate.status !== "pending") return;
      if (event.target.dataset.candidateAction === "add") openModal(null, candidate);
      if (event.target.dataset.candidateAction === "ignore") { candidate.status = "ignored"; saveCandidates(); render(); showToast(`已忽略“${candidate.value}”`); }
    });
    $("tagForm").addEventListener("submit", saveTag);
    $("closeModal").addEventListener("click", closeModal);
    $("cancelModal").addEventListener("click", closeModal);
    $("tagModal").addEventListener("click", (event) => { if (event.target === $("tagModal")) closeModal(); });
    $("confirmDeleteTag").addEventListener("click", deleteTag);
    $("closeDeleteTag").addEventListener("click", closeDeleteModal);
    $("cancelDeleteTag").addEventListener("click", closeDeleteModal);
    $("deleteTagModal").addEventListener("click", (event) => { if (event.target === $("deleteTagModal")) closeDeleteModal(); });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("deleteTagModal").classList.contains("hidden")) closeDeleteModal();
      else if (!$("tagModal").classList.contains("hidden")) closeModal();
    });
  }

  async function boot() {
    startClientLifecycle();
    bindEvents();
    try {
      const requested = query.get("workflow") || "";
      const endpoint = requested ? `/api/workflows?workflow_id=${encodeURIComponent(requested)}` : "/api/workflows";
      const response = await fetch(endpoint, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "工作流加载失败");
      state.workflows = payload.workflows || [];
      state.workflow = payload.active || state.workflows.find((workflow) => workflow.id === (payload.active_id || requested));
      if (!state.workflow) throw new Error("没有可用的工作流");
      state.tags = await loadValues(state.workflow.id);
      state.candidates = await loadCandidates(state.workflow);
      state.fieldId = fieldById(query.get("field"))?.id || fields()[0]?.id || "";
      render();
      if (query.get("candidate")) openModal(null, { fieldId: state.fieldId, value: query.get("candidate"), count: 1, examples: "当前解析结果", status: "pending" });
    } catch (error) {
      $("workflowDescription").textContent = error.message;
      showToast(error.message, true);
    }
  }

  boot();
})();
