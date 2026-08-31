(() => {
  "use strict";

  window.name = "main-app";

  const WORKFLOW_TAG_UPDATE_KEY = "offline-file-namer-workflow-tags-updated-v1";
  const FILE_TABLE_COLUMN_STORAGE_KEY = "offline-file-namer-file-table-column-widths-v1";

  const $ = (id) => document.getElementById(id);
  const state = { data: null, selectedExtensions: {}, exportExtensions: {}, exportAvailableExtensions: {}, rootDirty: false, scopeDirty: false, taskDirty: false };
  let toastTimer;
  let pendingRecordUpdate = Promise.resolve();
  let recordUpdateError = null;
  let audioDuration = 0;
  let pendingSeekRatio = null;
  let seekingAudio = false;
  let workflowRevision = 0;
  let workflowRefreshPending = false;

  function startClientLifecycle() {
    const heartbeat = async () => {
      try {
        const response = await fetch("/api/client-heartbeat", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: "{}",
          cache: "no-store",
          keepalive: true,
        });
        const payload = await response.json();
        const nextRevision = Number(payload.workflow_revision || 0);
        if (!nextRevision || nextRevision === workflowRevision) return;
        const hadRevision = workflowRevision > 0;
        if (!hadRevision) { workflowRevision = nextRevision; return; }
        if (workflowRefreshPending) return;
        workflowRefreshPending = true;
        try {
          const stateResponse = await fetch("/api/state", { cache: "no-store" });
          const statePayload = await stateResponse.json();
          if (statePayload.ok && statePayload.state) applyState(statePayload.state);
        } finally {
          workflowRefreshPending = false;
        }
      } catch (_error) {}
    };
    heartbeat();
    window.setInterval(heartbeat, 2000);
    window.addEventListener("pagehide", (event) => {
      // A persisted page is entering the back-forward cache, not closing.
      if (event.persisted) return;
      const body = new Blob(["{}"], { type: "application/json" });
      if (!navigator.sendBeacon || !navigator.sendBeacon("/api/client-closed", body)) {
        fetch("/api/client-closed", { method: "POST", body: "{}", keepalive: true }).catch(() => {});
      }
    });
  }

  function startWorkflowTagSync() {
    window.addEventListener("storage", (event) => {
      if (event.key !== WORKFLOW_TAG_UPDATE_KEY || !event.newValue) return;
      let update;
      try { update = JSON.parse(event.newValue); } catch (_error) { return; }
      const workflowId = String(update?.workflow_id || "");
      if (workflowId && workflowId !== String(state.data?.workflow?.active_id || "")) return;
      fetch("/api/state", { cache: "no-store" })
        .then((response) => response.json())
        .then((payload) => {
          if (payload.ok && payload.state) applyState(payload.state);
        })
        .catch(() => {});
    });
  }

  function updateSeekVisual(ratio) {
    const safeRatio = Math.max(0, Math.min(1, Number(ratio) || 0));
    $("audioSeek").style.setProperty("--audio-progress-position", `${safeRatio * 100}%`);
  }

  function applyTheme(theme) {
    const dark = theme === "dark";
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    $("themeIcon").textContent = dark ? "☀" : "☾";
    $("themeLabel").textContent = dark ? "浅色" : "深色";
    localStorage.setItem("offline-file-namer-theme", dark ? "dark" : "light");
  }

  async function api(path, options = {}) {
    const response = await fetch(path, options);
    const payload = await response.json().catch(() => ({ ok: false, error: "服务器返回无效数据" }));
    if (!response.ok || payload.ok === false) {
      if (payload.state) applyState(payload.state);
      throw new Error(payload.error || "请求失败");
    }
    return payload;
  }

  const jsonOptions = (body) => ({ method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const currentGroup = () => state.data?.groups?.find((group) => group.key === state.data.current_group_key);
  const groupExtensionLabel = (group) => group?.extension === ".image"
    ? "图像"
    : String(group?.extension || "").replace(".", "").toUpperCase() || "无扩展名";
  const currentRecords = () => state.data?.records || [];
  const normalRoot = (value) => String(value || "").trim().replace(/[\\/]+$/, "").toLowerCase();
  const scannedRootIsCurrent = () => !state.rootDirty && !state.scopeDirty && normalRoot($("rootPath").value) === normalRoot(state.data?.root);

  function updateActionAvailability() {
    const disabled = !state.data?.groups?.length || !scannedRootIsCurrent();
    ["previewButton", "fillWorkflowButton", "renameGroupButton", "renameAllButton", "importExcel", "applyMappingButton", "parsePreviewButton"].forEach((id) => { $(id).disabled = disabled; });
    document.querySelectorAll("[data-workflow-action], [data-workflow-module]").forEach((button) => { button.disabled = disabled; });
  }

  function requireCurrentScan() {
    if (scannedRootIsCurrent()) return true;
    showToast("根目录或扫描范围已修改，请先扫描", true);
    return false;
  }

  function showToast(message, error = false) {
    const toast = $("toast");
    toast.textContent = message;
    toast.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { toast.className = "toast"; }, 3400);
  }

  function applyState(next) {
    state.data = next;
    workflowRevision = Number(next.workflow?.revision || workflowRevision || 0);
    if (next.config?.theme && document.documentElement.dataset.theme !== next.config.theme) applyTheme(next.config.theme);
    const extensions = next.extensions || {};
    const enabledExtensions = next.extension_enabled || {};
    if (!state.rootDirty) state.exportAvailableExtensions = { ...extensions };
    Object.keys(extensions).forEach((ext) => {
      state.selectedExtensions[ext] = enabledExtensions[ext] !== false;
    });
    Object.keys(state.selectedExtensions).forEach((ext) => { if (!(ext in extensions)) delete state.selectedExtensions[ext]; });
    Object.keys(state.exportAvailableExtensions).forEach((ext) => {
      if (!(ext in state.exportExtensions)) state.exportExtensions[ext] = true;
    });
    Object.keys(state.exportExtensions).forEach((ext) => { if (!(ext in state.exportAvailableExtensions)) delete state.exportExtensions[ext]; });
    render();
  }

  function render() {
    const data = state.data || {};
    if (!state.rootDirty) $("rootPath").value = data.root || $("rootPath").value || "";
    if (!state.scopeDirty) {
      $("includeHidden").checked = !!data.include_hidden;
      $("includeSystem").checked = !!data.include_system;
    }
    $("groupCount").textContent = String((data.groups || []).length);
    $("fileMetric").textContent = String(data.total_file_count ?? (data.records || []).length);
    $("extensionMetric").textContent = String(Object.keys(data.extensions || {}).length);
    $("associationMetric").textContent = String((data.associations || []).length);
    const group = currentGroup();
    $("currentGroupMetric").textContent = group ? `${group.relative_folder || group.folder_name} / ${groupExtensionLabel(group)}` : "-";
    $("scanSummary").textContent = data.root ? `${data.root} · ${data.total_file_count ?? (data.records || []).length} 个文件` : "等待扫描";
    $("separator").value = data.separator ?? "_";
    $("nameMode").value = data.mode || "original";
    const numeric = data.numeric || {};
    $("numericStart").value = numeric.start ?? 1;
    $("numericWidth").value = numeric.width ?? 2;
    $("numericStep").value = numeric.step ?? 1;
    $("numericFields").classList.toggle("hidden", data.mode !== "numeric");
    renderWorkflowControls();
    $("parseTemplate").value = data.parse_template || "auto";
    $("parseUseName").checked = !!data.parse_use_name;
    renderMappingControls();
    renderGroups();
    renderExtensions();
    renderExportExtensions();
    renderRows();
    renderLogs();
    updateActionAvailability();
  }

  const modeLabels = { original: "原文件名", numeric: "数字编号", excel: "Excel 名称" };
  function activeWorkflow() { return state.data?.workflow?.active || {}; }
  function workflowFields() { return activeWorkflow().fields || []; }
  function tagValue(tag) { return typeof tag === "string" ? tag : String(tag?.value ?? ""); }
  function tagLabel(tag) { return typeof tag === "string" ? tag : String(tag?.label ?? tag?.value ?? ""); }

  function quickOptions(field, value, candidates = [], includePlaceholder = true, candidateDetails = []) {
    const options = includePlaceholder ? [`<option value="">标签</option>`] : [];
    const seen = new Set();
    const details = candidateDetails.reduce((map, item) => {
      const candidate = String(item?.value ?? "");
      if (candidate && !map[candidate]) map[candidate] = item.reason || `规则：${item.rule_id || "工作流"}`;
      return map;
    }, {});
    for (const candidate of candidates) {
      const candidateText = String(candidate ?? "");
      if (!candidateText || seen.has(candidateText)) continue;
      seen.add(candidateText);
      const reason = details[candidate] || "来自 metadata/解析结果";
      options.push(`<option value="${esc(candidateText)}" title="${esc(reason)}" ${candidateText === value ? "selected" : ""}>候选: ${esc(candidateText)}</option>`);
    }
    for (const tag of field.quick_tags || []) {
      const tagValueText = tagValue(tag);
      if (!tagValueText || seen.has(tagValueText)) continue;
      seen.add(tagValueText);
      options.push(`<option value="${esc(tagValueText)}" ${tagValueText === value ? "selected" : ""}>${esc(tagLabel(tag))}</option>`);
    }
    return options.join("");
  }

  function renderWorkflowControls() {
    const workflow = activeWorkflow();
    const parser = String(workflow.filename_parser || "");
    $("openTagManager").href = `/tag-manager?workflow=${encodeURIComponent(workflow.id || "default")}`;
    const select = $("workflowSelect");
    const available = state.data?.workflow?.available || [];
    select.innerHTML = available.map((item) => `<option value="${esc(item.id)}" ${item.id === workflow.id ? "selected" : ""}>${esc(item.name)}</option>`).join("");
    $("workflowDescription").textContent = workflow.description || "";
    $("parseTemplate").placeholder = parser === "sample_pack"
      ? "auto 或 {type}_{name}_{number}_{bpm}"
      : "auto 或 {type}_{name}_{number}";
    $("parseHelp").textContent = parser
      ? `当前工作流解析器：${parser}；解析只影响预览，除非勾选使用 name。`
      : "当前工作流仅使用通用文件名字段；解析只影响预览，除非勾选使用 name。";
    const allowedModes = workflow.name_modes || ["original"];
    $("nameMode").innerHTML = allowedModes.map((mode) => `<option value="${esc(mode)}" ${mode === state.data?.mode ? "selected" : ""}>${esc(modeLabels[mode] || mode)}</option>`).join("");
    const group = currentGroup();
    const workflowScopeFields = workflowFields().filter((field) => field.scope === "workflow");
    const groupFields = workflowFields().filter((field) => field.scope === "group");
    const panel = $("workflowFields");
    const renderField = (field, value, scope, candidates = []) => {
      const disabled = field.editable === false || field.kind === "fixed" ? "disabled" : "";
      const scopeAttribute = `data-workflow-scope="${esc(scope)}"`;
      if (field.kind === "choice") {
        return `<div class="field workflow-field-control"><label>${esc(field.label)}${field.required ? " *" : ""}</label><select data-workflow-group-quick="${esc(field.id)}" ${scopeAttribute} ${disabled}>${quickOptions(field, value, candidates, false)}</select></div>`;
      }
      const inputType = field.kind === "number" ? "number" : "text";
      const quick = (field.quick_tags?.length || candidates.length) ? `<select data-workflow-group-quick="${esc(field.id)}" ${scopeAttribute} ${disabled}>${quickOptions(field, value, candidates)}</select>` : "";
      return `<div class="field workflow-field-control"><label>${esc(field.label)}${field.required ? " *" : ""}</label><div class="quick-input"><input type="${inputType}" data-workflow-group-field="${esc(field.id)}" ${scopeAttribute} value="${esc(value)}" ${disabled}>${quick}</div></div>`;
    };
    const workflowValues = state.data?.workflow?.values || {};
    const workflowCandidates = state.data?.workflow?.candidates || {};
    const workflowMarkup = workflowScopeFields.map((field) => renderField(field, String(workflowValues[field.id] ?? field.default ?? ""), "workflow", workflowCandidates[field.id] || [])).join("");
    const groupValues = group?.workflow_values || {};
    const groupCandidates = group?.workflow_candidates || {};
    const groupMarkup = group ? groupFields.map((field) => renderField(field, String(groupValues[field.id] ?? field.default ?? ""), "group", groupCandidates[field.id] || [])).join("") : "";
    panel.innerHTML = workflowMarkup + groupMarkup;
    const actions = workflow.actions || [];
    const modules = (workflow.modules || []).filter((module) => module.trigger === "on_user_request");
    const actionMarkup = actions.map((action) => `<button class="button button-quiet" data-workflow-action="${esc(action.id)}" title="${esc(action.description || action.label)}"><span class="icon">↗</span>${esc(action.label)}</button>`).join("");
    const moduleMarkup = modules.map((module) => `<button class="button button-quiet" data-workflow-module="${esc(module.id)}" title="${esc(module.description || module.label)}"><span class="icon">↗</span>${esc(module.label)}</button>`).join("");
    $("workflowActions").innerHTML = actionMarkup + moduleMarkup;
  }

  function renderMappingControls() {
    const data = state.data || {};
    const mapping = data.directory_mapping || { meta: -3, group: -2, child: -1 };
    const maxDepth = Math.max(3, Number(data.max_depth || 0));
    const options = [{ value: "", label: "不使用" }, { value: "0", label: "根目录（0）" }];
    for (let level = 1; level <= maxDepth; level += 1) options.push({ value: String(level), label: `根下第 ${level} 级` });
    options.push({ value: "-1", label: "当前目录（-1）" });
    for (let up = 1; up <= Math.min(maxDepth + 1, 8); up += 1) options.push({ value: String(-(up + 1)), label: `向上 ${up} 级（-${up + 1}）` });
    [["metaLevel", "meta"], ["groupLevel", "group"], ["childLevel", "child"]].forEach(([id, key]) => {
      const select = $(id);
      const expected = mapping[key] === null || mapping[key] === undefined ? "" : String(mapping[key]);
      select.innerHTML = options.map((item) => `<option value="${item.value}" ${item.value === expected ? "selected" : ""}>${esc(item.label)}</option>`).join("");
      select.title = data.directory_mapping_auto ? "当前使用自动末端三级预设" : "当前使用自定义目录层级映射";
    });
  }

  function mappingPayload() {
    const parseLevel = (id) => $(id).value === "" ? null : Number($(id).value);
    return { meta: parseLevel("metaLevel"), group: parseLevel("groupLevel"), child: parseLevel("childLevel") };
  }

  function renderGroups() {
    const container = $("groupList");
    const groups = state.data?.groups || [];
    if (!groups.length) { container.className = "group-list empty-state"; container.textContent = "扫描后显示命名组"; return; }
    container.className = "group-list";
    container.innerHTML = groups.map((group) => `
      <div class="group-row ${group.key === state.data.current_group_key ? "active" : ""}" data-group-key="${esc(group.key)}">
        <button class="group-toggle" data-toggle-group="${esc(group.key)}" title="切换是否加入全部执行">${group.enabled ? "☑" : "☐"}</button>
        <span class="group-text" title="${esc(group.folder)}">${esc(group.relative_folder || group.folder_name)} / ${esc(groupExtensionLabel(group))}</span>
        <span class="group-count">${group.count}</span>
      </div>`).join("");
  }

  function chipHtml(ext, count, checked) {
    const label = ext ? ext.replace(".", "").toUpperCase() : "无扩展名";
    return `<label class="chip"><input type="checkbox" data-ext="${esc(ext)}" ${checked ? "checked" : ""}>${esc(label)} <span>${count}</span></label>`;
  }

  function renderExtensions() {
    const extensions = state.data?.extensions || {};
    const keys = Object.keys(extensions);
    $("extensionFilters").innerHTML = keys.length ? keys.map((ext) => chipHtml(ext, extensions[ext], state.selectedExtensions[ext] !== false)).join("") : '<span class="muted">扫描后动态生成扩展名</span>';
  }

  function renderExportExtensions() {
    const extensions = state.exportAvailableExtensions || {};
    const keys = Object.keys(extensions);
    $("exportExtensions").innerHTML = keys.length ? keys.map((ext) => chipHtml(ext, extensions[ext], state.exportExtensions[ext] !== false)).join("") : '<span class="muted">点击刷新扫描</span>';
  }

  function statusClass(status) {
    if (["Conflict", "Error"].includes(status)) return "error";
    if (["Skipped", "未匹配"].includes(status)) return "warn";
    if (["Ready", "Renamed", "Unchanged"].includes(status)) return "ready";
    return "";
  }

  function renderWorkflowRecordFields(record) {
    const fields = workflowFields().filter((field) => ["record", "suffix"].includes(field.scope));
    if (!fields.length) return '<span class="muted">—</span>';
    return `<div class="workflow-record-fields">${fields.map((field) => {
      const value = String(record.workflow_values?.[field.id] ?? field.default ?? "");
      const candidates = record.workflow_candidates?.[field.id] || [];
      const candidateDetails = record.workflow_candidate_details?.[field.id] || [];
      const nameLocked = field.id === "name" && (state.data?.mode !== "original" || state.data?.parse_use_name);
      const disabled = field.editable === false || field.kind === "fixed" || nameLocked ? "disabled" : "";
      if (field.kind === "choice") {
        return `<div class="workflow-record-field"><label title="${esc(field.label)}">${esc(field.label)}${field.required ? " *" : ""}</label><select data-workflow-record-quick="${esc(field.id)}" ${disabled}>${quickOptions(field, value, candidates, true, candidateDetails)}</select></div>`;
      }
      const inputType = field.kind === "number" ? "number" : "text";
      const quick = (field.quick_tags?.length || candidates.length) ? `<select data-workflow-record-quick="${esc(field.id)}" ${disabled}>${quickOptions(field, value, candidates, true, candidateDetails)}</select>` : "";
      return `<div class="workflow-record-field"><label title="${esc(field.label)}">${esc(field.label)}${field.required ? " *" : ""}</label><div class="record-input-wrap"><input class="workflow-record-input" type="${inputType}" data-workflow-record-field="${esc(field.id)}" value="${esc(value)}" ${disabled}>${quick}</div></div>`;
    }).join("")}</div>`;
  }

  function renderRows() {
    const body = $("fileRows");
    const search = $("searchInput").value.trim().toLowerCase();
    const status = $("statusFilter").value;
    const rows = currentRecords().filter((record) => {
      if (record.removed) return false;
      if (search && !`${record.original_name} ${record.target_name}`.toLowerCase().includes(search)) return false;
      return status === "all" || record.status === status;
    });
    if (!rows.length) { body.innerHTML = '<tr><td colspan="11" class="empty-row">当前筛选没有文件</td></tr>'; $("selectAll").checked = false; $("selectAll").indeterminate = false; updateSelectedMetric(); return; }
    body.innerHTML = rows.map((record) => {
      const rowClass = statusClass(record.status) === "error" ? "row-error" : statusClass(record.status) === "warn" ? "row-warning" : "";
      const play = record.is_audio ? '<button class="row-action" data-play="1" title="播放音频">▶</button>' : '<span class="muted">—</span>';
      const metadataItems = [];
      const flattenMetadata = (value, prefix = "") => {
        if (value && typeof value === "object" && !Array.isArray(value)) {
          Object.entries(value).forEach(([key, child]) => flattenMetadata(child, prefix ? `${prefix}.${key}` : key));
        } else if (prefix && value !== null && value !== undefined && String(value) !== "") {
          metadataItems.push(`${prefix}: ${value}`);
        }
      };
      flattenMetadata(record.metadata);
      const metadataSummary = metadataItems.filter((item) => !item.startsWith("file.")).join(" · ");
      const ruleReasons = [...new Set(Object.values(record.workflow_candidate_details || {}).flatMap((items) => items.map((item) => item.reason).filter(Boolean)))].join("；");
      const parsedFields = [metadataSummary, ruleReasons, ...Object.entries(record.parsed_fields || {}).filter(([, value]) => value).map(([key, value]) => `${key}: ${value}`)].filter(Boolean).join(" · ");
      const parsed = parsedFields ? `${parsedFields} · ${(Number(record.parse_confidence || 0) * 100).toFixed(0)}%` : "";
      const association = (record.associated_extensions || []).length > 1 ? `<span class="association-tag" title="同目录、同 stem 的跨格式关联；重命名与撤销同步">关联 ${record.associated_extensions.map((ext) => ext.replace(".", "").toUpperCase()).join("/")}</span>` : "";
      return `<tr draggable="true" class="${rowClass}" data-path="${esc(record.path)}">
        <td class="check-col"><input class="row-check" type="checkbox" data-select-record="1" ${record.selected ? "checked" : ""}></td>
        <td><div class="file-name" title="${esc(record.original_name)}">${esc(record.original_name)}</div>${association}</td>
        <td><div class="folder-cell" title="${esc(record.relative_folder)}">${esc(record.relative_folder)}</div></td>
        <td class="ext-label">${esc(record.extension || "无")}</td>
        <td>${renderWorkflowRecordFields(record)}</td>
        <td><div class="parse-cell" title="${esc(record.parse_error || parsed || record.parse_unmatched)}">${esc(record.parse_error ? `错误: ${record.parse_error}` : (parsed || (record.parse_unmatched ? `未匹配: ${record.parse_unmatched}` : "—")))}</div></td>
        <td class="preview-cell" title="${esc(record.target_name)}">${esc(record.target_name || "点击刷新预览")}</td>
        <td><span class="status-tag ${statusClass(record.status)}" title="${esc(record.status_detail)}">${esc(record.status)}</span></td>
        <td class="action-col">${play}</td>
        <td class="action-col"><button class="row-action" data-rename="1" title="单项重命名">↗</button></td>
        <td class="action-col"><button class="row-action" data-remove="1" title="从任务移除">×</button></td>
      </tr>`;
    }).join("");
    const visibleSelected = rows.filter((record) => record.selected).length;
    $("selectAll").checked = rows.length > 0 && visibleSelected === rows.length;
    $("selectAll").indeterminate = visibleSelected > 0 && visibleSelected < rows.length;
    updateSelectedMetric();
  }

  function updateSelectedMetric() {
    const selected = currentRecords().filter((record) => record.selected && !record.removed).length;
    $("selectedMetric").textContent = `已选 ${selected}`;
  }

  function renderLogs() {
    const logs = state.data?.logs || [];
    $("logCount").textContent = String(logs.length);
    $("logBody").innerHTML = logs.map((entry) => `<div class="log-entry ${esc(entry.level)}">[${esc(entry.timestamp.slice(11, 19))}] [${esc(entry.level)}] ${esc(entry.message)}</div>`).join("");
    $("logBody").scrollTop = $("logBody").scrollHeight;
  }

  async function scan(rootOverride = null, forExport = false) {
    if (forExport) {
      const root = rootOverride || $("rootPath").value.trim();
      try {
        const payload = await api("/api/export-scan", jsonOptions({ root, include_hidden: $("exportHidden").checked, include_system: $("exportSystem").checked }));
        state.exportAvailableExtensions = payload.extensions || {};
        const nextSelection = {};
        Object.keys(state.exportAvailableExtensions).forEach((ext) => { nextSelection[ext] = state.exportExtensions[ext] !== false; });
        state.exportExtensions = nextSelection;
        renderExportExtensions();
        showToast(`导出范围扫描完成，发现 ${payload.total_file_count} 个文件`);
      } catch (error) { showToast(error.message, true); }
      return;
    }
    if (!await flushRecordUpdates()) return;
    const root = rootOverride || $("rootPath").value.trim();
    if (state.taskDirty && state.data?.groups?.length && !window.confirm("重新扫描会清除当前名称、选择和 Excel 映射。继续吗？")) return;
    try {
      const payload = await api("/api/scan", jsonOptions({ root, include_hidden: $("includeHidden").checked, include_system: $("includeSystem").checked, directory_mapping: state.data?.directory_mapping_auto === false ? mappingPayload() : null }));
      state.rootDirty = false;
      state.scopeDirty = false;
      state.taskDirty = false;
      applyState(payload.state);
      showToast(`扫描完成，发现 ${payload.state.total_file_count ?? payload.state.records.length} 个文件`);
    } catch (error) { showToast(error.message, true); }
  }

  function previewPayload() {
    const group = currentGroup();
    return { group_key: group?.key, separator: $("separator").value, mode: $("nameMode").value, parse_template: $("parseTemplate").value, parse_use_name: $("parseUseName").checked,
      numeric: { start: $("numericStart").value, width: $("numericWidth").value, step: $("numericStep").value }, extensions: Object.keys(state.selectedExtensions).filter((ext) => state.selectedExtensions[ext]) };
  }

  async function refreshPreview(silent = false, skipPending = false) {
    if (!skipPending && !await flushRecordUpdates()) return false;
    if (!requireCurrentScan()) return false;
    if (!state.data?.groups?.length) { if (!silent) showToast("请先扫描根目录", true); return; }
    try { const payload = await api("/api/preview", jsonOptions(previewPayload())); applyState(payload.state); if (!silent) showToast("目标文件名预览已更新"); return true; }
    catch (error) { showToast(error.message, true); return false; }
  }

  async function updateRecord(path, changes, preview = true) {
    try { const payload = await api("/api/record", jsonOptions({ path, ...changes })); recordUpdateError = null; state.taskDirty = true; applyState(payload.state); if (preview) await refreshPreview(true, true); }
    catch (error) { recordUpdateError = error; showToast(error.message, true); }
  }

  function queueRecordUpdate(path, changes, preview = true) {
    state.taskDirty = true;
    recordUpdateError = null;
    pendingRecordUpdate = pendingRecordUpdate.catch(() => {}).then(() => updateRecord(path, changes, preview));
    return pendingRecordUpdate;
  }

  async function flushRecordUpdates() {
    await pendingRecordUpdate.catch(() => {});
    if (!recordUpdateError) return true;
    recordUpdateError = null;
    return false;
  }

  async function updateWorkflowValue(field, value, path = "") {
    try {
      const group = currentGroup();
      const payload = await api("/api/workflow-value", jsonOptions({ group_key: group?.key || "", field, value, path }));
      recordUpdateError = null;
      state.taskDirty = true;
      applyState(payload.state);
    } catch (error) {
      recordUpdateError = error;
      showToast(error.message, true);
    }
  }

  function queueWorkflowValue(field, value, path = "") {
    state.taskDirty = true;
    recordUpdateError = null;
    pendingRecordUpdate = pendingRecordUpdate.catch(() => {}).then(() => updateWorkflowValue(field, value, path));
    return pendingRecordUpdate;
  }

  async function selectWorkflow(workflowId) {
    if (!await flushRecordUpdates()) return;
    try {
      const payload = await api("/api/workflow/select", jsonOptions({ workflow_id: workflowId }));
      state.taskDirty = true;
      applyState(payload.state);
      showToast(`已切换工作流：${payload.workflow.active.name}`);
    } catch (error) { showToast(error.message, true); }
  }

  async function importWorkflow(file) {
    if (!file) return;
    try {
      const packageFile = file.name.toLowerCase().endsWith(".ffnf-workflow");
      const trustModules = packageFile && window.confirm("工作流包可能包含会在本机执行的 Python 模块。仅在确认来源可信时继续安装。");
      if (packageFile && !trustModules) return;
      const form = new FormData();
      form.append("file", file);
      form.append("strategy", "copy");
      form.append("trust_modules", trustModules ? "true" : "false");
      const payload = await api("/api/workflow/import", { method: "POST", body: form });
      state.taskDirty = true;
      applyState(payload.state);
      showToast(`已导入工作流：${payload.imported.name}${payload.copied ? "（已作为副本）" : ""}`);
    } catch (error) { showToast(error.message, true); }
  }

  async function exportWorkflow() {
    const workflowId = activeWorkflow().id || "default";
    try {
      const response = await fetch(`/api/workflow-export?workflow_id=${encodeURIComponent(workflowId)}`);
      if (!response.ok) throw new Error("工作流导出失败");
      const blob = await response.blob();
      const link = document.createElement("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${workflowId}.ffnf-workflow`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(link.href);
      showToast("工作流已导出");
    } catch (error) { showToast(error.message, true); }
  }

  async function selectVisible(selected) {
    if (!await flushRecordUpdates()) return;
    const search = $("searchInput").value.trim().toLowerCase();
    const status = $("statusFilter").value;
    const paths = currentRecords().filter((record) => !record.removed && (!search || `${record.original_name} ${record.target_name}`.toLowerCase().includes(search)) && (status === "all" || record.status === status)).map((record) => record.path);
    const payload = await api("/api/records-batch", jsonOptions({ updates: paths.map((path) => ({ path, selected })) }));
    state.taskDirty = true; applyState(payload.state);
  }

  async function removeSelected() {
    if (!await flushRecordUpdates()) return;
    const paths = currentRecords().filter((record) => record.selected && !record.removed).map((record) => record.path);
    if (!paths.length) { showToast("没有选中的文件", true); return; }
    const payload = await api("/api/records-batch", jsonOptions({ updates: paths.map((path) => ({ path, selected: false, removed: true })) }));
    state.taskDirty = true; applyState(payload.state); showToast(`已移除 ${paths.length} 个任务行，磁盘文件未删除`);
  }

  async function rename(scope, path = null) {
    try {
      if (!await flushRecordUpdates()) return;
      if (!requireCurrentScan()) return;
      if (!await refreshPreview(true, true)) return;
      const payload = await api("/api/rename", jsonOptions({ scope, path, root: $("rootPath").value.trim() }));
      state.taskDirty = false;
      applyState(payload.state);
      showToast(`重命名完成：成功 ${payload.success}，失败/冲突 ${payload.failed}` , payload.failed > 0);
    } catch (error) { showToast(error.message, true); }
  }

  async function importExcel(file) {
    if (!await flushRecordUpdates()) return;
    if (!file || !currentGroup() || !requireCurrentScan()) return;
    try {
      const form = new FormData(); form.append("file", file); form.append("group_key", currentGroup().key);
      const payload = await api("/api/import-excel", { method: "POST", body: form });
      state.taskDirty = true;
      applyState(payload.state);
      showToast(`Excel ${payload.match.sheet ? `[${payload.match.sheet}] ` : ""}匹配：成功 ${payload.match.matched}，未匹配文件 ${payload.match.unmatched_files}，未匹配行 ${payload.match.unmatched_rows}`, payload.match.unmatched_files > 0 || payload.match.unmatched_rows > 0);
    } catch (error) { showToast(error.message, true); }
  }

  async function exportTables() {
    if (!await flushRecordUpdates()) return;
    const extensions = Object.keys(state.exportExtensions).filter((ext) => state.exportExtensions[ext]);
    if (!extensions.length) { showToast("至少选择一种导出扩展名", true); return; }
    try {
      const payload = await api("/api/export", jsonOptions({ root: $("rootPath").value.trim(), extensions, include_hidden: $("exportHidden").checked, include_system: $("exportSystem").checked }));
      applyState(payload.state);
      $("exportResults").className = "export-results";
      const xlsxOutputs = payload.xlsx_outputs || payload.outputs.filter((path) => path.toLowerCase().endsWith(".xlsx"));
      const filetree = payload.filetree_output || payload.outputs.find((path) => path.toLowerCase().endsWith("filetree.txt"));
      const outputRows = payload.outputs.map((path) => `<div class="result-file"><span class="result-kind">${path.toLowerCase().endsWith(".xlsx") ? "XLSX" : "结构"}</span>${esc(path)}</div>`).join("");
      const stats = payload.export_stats || {};
      const statsRow = `<div class="result-file"><span class="result-kind">统计</span>${esc(stats.directory_count || 0)} 个目录 · ${esc(stats.file_count || 0)} 个文件 · ${esc(stats.content_directory_count || 0)} 个内容目录 · ${esc(stats.empty_directory_count || 0)} 个空目录</div>`;
      $("exportResults").innerHTML = payload.outputs.length ? statsRow + outputRows : "没有符合条件的文件夹";
      showToast(`导出完成：${xlsxOutputs.length} 个 XLSX${filetree ? "，已生成目录索引" : ""}`);
      // Opening Explorer is a local convenience; failures are not fatal to export.
      fetch("/api/open-root", jsonOptions({ root: $("rootPath").value.trim() })).catch(() => {});
    } catch (error) { showToast(error.message, true); }
  }

  async function reorderRows(source, target) {
    if (!await flushRecordUpdates()) return;
    const order = currentRecords().map((record) => record.path);
    const from = order.indexOf(source); const to = order.indexOf(target);
    if (from < 0 || to < 0 || from === to) return;
    order.splice(from, 1); order.splice(to, 0, source);
    try { const payload = await api("/api/reorder", jsonOptions({ paths: order })); state.taskDirty = true; applyState(payload.state); }
    catch (error) { showToast(error.message, true); }
  }

  function bindEvents() {
    setupLogResizer();
    setupSidebarResizer();
    setupFileTableColumnResizers();
    applyTheme(localStorage.getItem("offline-file-namer-theme") || "light");
    $("themeToggle").addEventListener("click", () => {
      const theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      applyTheme(theme);
      api("/api/config", jsonOptions({ theme })).catch(() => {});
    });
    $("scanButton").addEventListener("click", () => scan());
    $("rootPath").addEventListener("keydown", (event) => { if (event.key === "Enter") scan(); });
    $("rootPath").addEventListener("input", () => { state.rootDirty = normalRoot($("rootPath").value) !== normalRoot(state.data?.root); updateActionAvailability(); });
    $("pickFolder").addEventListener("click", async () => {
      try {
        if (!await flushRecordUpdates()) return;
        const payload = await api("/api/pick-folder", jsonOptions({}));
        if (payload.path) {
          $("rootPath").value = payload.path;
          state.rootDirty = normalRoot(payload.path) !== normalRoot(state.data?.root);
          updateActionAvailability();
          await scan(payload.path);
        }
      }
      catch (error) { showToast(error.message, true); }
    });
    $("includeHidden").addEventListener("change", () => { state.scopeDirty = $("includeHidden").checked !== !!state.data?.include_hidden || $("includeSystem").checked !== !!state.data?.include_system; updateActionAvailability(); showToast("扫描范围已修改，点击扫描应用"); });
    $("includeSystem").addEventListener("change", () => { state.scopeDirty = $("includeHidden").checked !== !!state.data?.include_hidden || $("includeSystem").checked !== !!state.data?.include_system; updateActionAvailability(); showToast("扫描范围已修改，点击扫描应用"); });
    $("groupList").addEventListener("click", async (event) => {
      const toggle = event.target.closest("[data-toggle-group]");
      const row = event.target.closest("[data-group-key]");
      try {
        if (!await flushRecordUpdates()) return;
        if (toggle) { const payload = await api("/api/toggle-group", jsonOptions({ key: toggle.dataset.toggleGroup })); state.taskDirty = true; applyState(payload.state); return; }
        if (row) { const payload = await api("/api/select-group", jsonOptions({ key: row.dataset.groupKey })); applyState(payload.state); }
      } catch (error) { showToast(error.message, true); }
    });
    $("extensionFilters").addEventListener("change", (event) => { if (event.target.matches("[data-ext]")) { state.selectedExtensions[event.target.dataset.ext] = event.target.checked; state.taskDirty = true; refreshPreview(true); } });
    $("exportExtensions").addEventListener("change", (event) => { if (event.target.matches("[data-ext]")) state.exportExtensions[event.target.dataset.ext] = event.target.checked; });
    ["separator", "numericStart", "numericWidth", "numericStep"].forEach((id) => $(id).addEventListener("change", () => { state.taskDirty = true; refreshPreview(); }));
    $("nameMode").addEventListener("change", () => { state.taskDirty = true; $("numericFields").classList.toggle("hidden", $("nameMode").value !== "numeric"); refreshPreview(); });
    $("workflowSelect").addEventListener("change", (event) => selectWorkflow(event.target.value));
    $("workflowFields").addEventListener("change", (event) => {
      const quick = event.target.closest("[data-workflow-group-quick]");
      if (!quick) return;
      const input = quick.parentElement.querySelector("[data-workflow-group-field]");
      if (input && quick.value) input.value = quick.value;
      if (quick.dataset.workflowScope === "workflow" || quick.value) {
        queueWorkflowValue(quick.dataset.workflowGroupQuick, quick.value);
      }
    });
    $("workflowFields").addEventListener("blur", (event) => {
      const input = event.target.closest("[data-workflow-group-field]");
      if (input) queueWorkflowValue(input.dataset.workflowGroupField, input.value);
    }, true);
    $("importWorkflow").addEventListener("click", () => $("workflowFile").click());
    $("workflowFile").addEventListener("change", async (event) => { await importWorkflow(event.target.files[0]); event.target.value = ""; });
    $("exportWorkflow").addEventListener("click", () => exportWorkflow());
    $("previewButton").addEventListener("click", () => refreshPreview());
    $("fillWorkflowButton").addEventListener("click", async () => {
      try {
        if (!await flushRecordUpdates() || !requireCurrentScan()) return;
        const payload = await api("/api/workflow-fill", jsonOptions({}));
        state.taskDirty = true;
        applyState(payload.state);
        showToast(payload.filled ? `已填充 ${payload.filled} 个自动值` : "没有可填充的自动值");
      } catch (error) { showToast(error.message, true); }
    });
    $("workflowActions").addEventListener("click", async (event) => {
      const moduleButton = event.target.closest("[data-workflow-module]");
      if (moduleButton) {
        try {
          if (!await flushRecordUpdates() || !requireCurrentScan()) return;
          const payload = await api("/api/workflow-module/run", jsonOptions({ module_id: moduleButton.dataset.workflowModule }));
          state.taskDirty = true;
          applyState(payload.state);
          showToast(`已分析 ${payload.processed} 个文件，新增 ${payload.candidates_added} 个候选标签`);
        } catch (error) { showToast(error.message, true); }
        return;
      }
      const button = event.target.closest("[data-workflow-action]");
      if (!button) return;
      try {
        if (!await flushRecordUpdates()) return;
        const group = currentGroup();
        if (!group || !requireCurrentScan()) { if (!group) showToast("请先扫描并选择命名组", true); return; }
        const payload = await api("/api/workflow-action", jsonOptions({ group_key: group.key, action_id: button.dataset.workflowAction }));
        state.taskDirty = true;
        applyState(payload.state);
        showToast(`${payload.action?.label || "工作流动作"}：应用 ${payload.added} 个${payload.missing ? `，缺少值 ${payload.missing} 个` : ""}`, payload.missing > 0);
      } catch (error) { showToast(error.message, true); }
    });
    $("applyMappingButton").addEventListener("click", async () => {
      try {
        if (!await flushRecordUpdates() || !requireCurrentScan()) return;
        const payload = await api("/api/directory-mapping", jsonOptions({ mapping: mappingPayload() })); state.taskDirty = true; applyState(payload.state); showToast("目录层级映射已应用");
      }
      catch (error) { showToast(error.message, true); }
    });
    $("parsePreviewButton").addEventListener("click", async () => {
      try {
        if (!await flushRecordUpdates()) return;
        const group = currentGroup();
        if (!group || !requireCurrentScan()) { if (!group) showToast("请先扫描并选择命名组", true); return; }
        const payload = await api("/api/parse-preview", jsonOptions({ group_key: group.key, template: $("parseTemplate").value, use_name: $("parseUseName").checked }));
        state.taskDirty = true;
        applyState(payload.state);
        if ($("parseUseName").checked) await refreshPreview(true);
        showToast(`解析预览完成：${payload.parsed.length} 个文件`);
      } catch (error) { showToast(error.message, true); }
    });
     $("renameGroupButton").addEventListener("click", () => rename("group"));
     $("renameAllButton").addEventListener("click", () => rename("all"));
     $("undoButton").addEventListener("click", async () => { try { if (!await flushRecordUpdates()) return; const payload = await api("/api/undo", jsonOptions({})); state.taskDirty = false; applyState(payload.state); showToast(payload.ok ? "最近一次重命名已撤销" : "撤销未完成", !payload.ok); } catch (error) { showToast(error.message, true); } });
     $("redoButton").addEventListener("click", async () => { try { if (!await flushRecordUpdates()) return; const payload = await api("/api/redo", jsonOptions({})); state.taskDirty = false; applyState(payload.state); showToast(payload.ok ? "最近一次撤销已还原" : "还原未完成", !payload.ok); } catch (error) { showToast(error.message, true); } });
    $("selectVisible").addEventListener("click", () => selectVisible(true).catch((error) => showToast(error.message, true)));
    $("deselectVisible").addEventListener("click", () => selectVisible(false).catch((error) => showToast(error.message, true)));
    $("removeSelected").addEventListener("click", () => removeSelected().catch((error) => showToast(error.message, true)));
    $("selectAll").addEventListener("change", (event) => selectVisible(event.target.checked).catch((error) => showToast(error.message, true)));
    $("searchInput").addEventListener("input", renderRows);
    $("statusFilter").addEventListener("change", renderRows);
    $("fileRows").addEventListener("change", async (event) => {
      const row = event.target.closest("tr[data-path]"); if (!row) return;
      try {
        if (event.target.matches("[data-select-record]")) await queueRecordUpdate(row.dataset.path, { selected: event.target.checked }, false);
        if (event.target.matches("[data-record-field]")) await queueRecordUpdate(row.dataset.path, { [event.target.dataset.recordField]: event.target.value }, true);
        if (event.target.matches("[data-workflow-record-quick]")) {
           const input = event.target.closest(".record-input-wrap")?.querySelector("[data-workflow-record-field]");
           if (input && event.target.value) input.value = event.target.value;
           if (!input || event.target.value) await queueWorkflowValue(event.target.dataset.workflowRecordQuick, event.target.value, row.dataset.path);
        }
      } catch (_error) {}
    });
    $("fileRows").addEventListener("blur", (event) => {
      const input = event.target.closest("[data-workflow-record-field]");
      const row = event.target.closest("tr[data-path]");
      if (input && row) queueWorkflowValue(input.dataset.workflowRecordField, input.value, row.dataset.path);
    }, true);
    $("fileRows").addEventListener("click", async (event) => {
      const row = event.target.closest("tr[data-path]"); if (!row) return;
      try {
        if (event.target.closest("[data-play]")) {
          const record = currentRecords().find((item) => item.path === row.dataset.path); if (record) { const audio = $("audioPlayer"); pendingSeekRatio = null; audioDuration = 0; audio.src = `/audio?path=${encodeURIComponent(record.path)}`; audio.load(); audio.play().catch(() => showToast("当前浏览器无法解码此音频格式", true)); $("audioName").textContent = record.original_name; $("audioMeta").textContent = record.relative_folder; $("audioSeek").value = "0"; updateSeekVisual(0); $("audioCurrent").textContent = "00:00"; $("audioDuration").textContent = "00:00"; }
        } else if (event.target.closest("[data-rename]")) await rename("single", row.dataset.path);
        else if (event.target.closest("[data-remove]")) { if (!await flushRecordUpdates()) return; await api("/api/remove", jsonOptions({ path: row.dataset.path })); const payload = await api("/api/state"); state.taskDirty = true; applyState(payload.state); }
      } catch (error) { showToast(error.message, true); }
    });
    $("fileRows").addEventListener("dragstart", (event) => { const row = event.target.closest("tr[data-path]"); if (row) { event.dataTransfer.effectAllowed = "move"; event.dataTransfer.setData("text/plain", row.dataset.path); } });
    $("fileRows").addEventListener("dragover", (event) => { if (event.target.closest("tr[data-path]")) event.preventDefault(); });
    $("fileRows").addEventListener("drop", (event) => { event.preventDefault(); const row = event.target.closest("tr[data-path]"); const source = event.dataTransfer.getData("text/plain"); if (row && source) reorderRows(source, row.dataset.path); });
    $("importExcel").addEventListener("click", () => $("excelFile").click());
    $("excelFile").addEventListener("change", async (event) => { await importExcel(event.target.files[0]); event.target.value = ""; });
    const audio = $("audioPlayer");
    audio.volume = Number($("audioVolume").value);
    $("audioPlay").addEventListener("click", () => {
      if (!audio.src) { showToast("请先选择音频文件", true); return; }
      if (audio.paused) audio.play().catch(() => showToast("当前浏览器无法解码此音频格式", true));
      else audio.pause();
    });
    $("audioSeek").addEventListener("input", (event) => {
      seekAudio(Number(event.target.value) / 1000);
    });
    $("audioSeek").addEventListener("change", (event) => { seekAudio(Number(event.target.value) / 1000); });
    $("audioSeek").addEventListener("pointerdown", () => { seekingAudio = true; });
    $("audioSeek").addEventListener("pointerup", () => { seekingAudio = false; });
    $("audioSeek").addEventListener("pointercancel", () => { seekingAudio = false; });
    $("audioSeek").addEventListener("mousedown", () => { seekingAudio = true; });
    $("audioSeek").addEventListener("mouseup", () => { seekingAudio = false; });
    $("audioVolume").addEventListener("input", (event) => { audio.volume = Number(event.target.value); });
    audio.addEventListener("loadedmetadata", updateAudioMetadata);
    audio.addEventListener("durationchange", updateAudioMetadata);
    audio.addEventListener("timeupdate", () => { const current = Number.isFinite(audio.currentTime) ? audio.currentTime : 0; $("audioCurrent").textContent = formatTime(current); if (!seekingAudio) { const ratio = audioDuration > 0 ? current / audioDuration : 0; $("audioSeek").value = String(Math.round(ratio * 1000)); updateSeekVisual(ratio); } });
    audio.addEventListener("play", () => { $("audioPlay").textContent = "Ⅱ"; });
    audio.addEventListener("pause", () => { $("audioPlay").textContent = "▶"; });
    audio.addEventListener("ended", () => { $("audioPlay").textContent = "▶"; $("audioSeek").value = "0"; updateSeekVisual(0); });
    audio.addEventListener("error", () => showToast("当前浏览器无法解码此音频格式", true));
    document.querySelectorAll(".view-tab").forEach((tab) => tab.addEventListener("click", () => { document.querySelectorAll(".view-tab").forEach((item) => item.classList.toggle("active", item === tab)); document.querySelectorAll(".view-panel").forEach((panel) => panel.classList.toggle("active", panel.id === `${tab.dataset.view}View`)); }));
    $("exportScanButton").addEventListener("click", () => scan(null, true));
    $("exportButton").addEventListener("click", () => exportTables());
    $("logToggle").addEventListener("click", () => $("logDrawer").classList.toggle("collapsed"));
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return "00:00";
    const total = Math.floor(seconds);
    const hours = Math.floor(total / 3600);
    const minutes = Math.floor((total % 3600) / 60);
    const remainder = total % 60;
    return hours > 0 ? `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
  }

  function updateAudioMetadata() {
    const audio = $("audioPlayer");
    audioDuration = Number.isFinite(audio.duration) ? audio.duration : 0;
    $("audioDuration").textContent = formatTime(audioDuration);
    if (pendingSeekRatio !== null && audioDuration > 0) {
      audio.currentTime = Math.max(0, Math.min(1, pendingSeekRatio)) * audioDuration;
      pendingSeekRatio = null;
    }
  }

  function seekAudio(ratio) {
    const safeRatio = Math.max(0, Math.min(1, Number(ratio) || 0));
    if (audioDuration > 0 && Number.isFinite(audioDuration)) {
    $("audioPlayer").currentTime = safeRatio * audioDuration;
      pendingSeekRatio = null;
    } else {
      pendingSeekRatio = safeRatio;
    }
    updateSeekVisual(safeRatio);
  }

  function setupLogResizer() {
    const drawer = $("logDrawer");
    const resizer = $("logResizer");
    const savedHeight = Number(localStorage.getItem("offline-file-namer-log-height"));
    if (Number.isFinite(savedHeight) && savedHeight >= 70) {
      document.documentElement.style.setProperty("--log-height", `${savedHeight}px`);
    }
    let dragging = false;
    let startY = 0;
    let startHeight = 0;
    const beginResize = (event) => {
      if (dragging) return;
      if (drawer.classList.contains("collapsed")) return;
      dragging = true;
      startY = event.clientY;
      startHeight = $("logBody").getBoundingClientRect().height;
      resizer.classList.add("resizing");
      if (event.pointerId !== undefined) resizer.setPointerCapture(event.pointerId);
      document.body.style.userSelect = "none";
    };
    const moveResize = (event) => {
      if (!dragging) return;
      const nextHeight = Math.max(70, Math.min(Math.round(window.innerHeight * .65), startHeight + startY - event.clientY));
      document.documentElement.style.setProperty("--log-height", `${nextHeight}px`);
      localStorage.setItem("offline-file-namer-log-height", String(nextHeight));
    };
    const stopResize = () => {
      dragging = false;
      resizer.classList.remove("resizing");
      document.body.style.userSelect = "";
    };
    resizer.addEventListener("pointerdown", beginResize);
    resizer.addEventListener("pointermove", moveResize);
    resizer.addEventListener("pointerup", stopResize);
    resizer.addEventListener("pointercancel", stopResize);
    // Mouse fallbacks keep the handle working in older embedded WebViews.
    resizer.addEventListener("mousedown", beginResize);
    document.addEventListener("mousemove", moveResize);
    document.addEventListener("mouseup", stopResize);
  }

  function setupSidebarResizer() {
    const handle = $("sidebarResizer");
    const savedWidth = Number(localStorage.getItem("offline-file-namer-sidebar-width"));
    const minWidth = 220;
    const maxWidth = 420;
    if (Number.isFinite(savedWidth)) {
      document.documentElement.style.setProperty("--sidebar-width", `${Math.max(minWidth, Math.min(maxWidth, savedWidth))}px`);
    }
    let dragging = false;
    const setWidth = (clientX) => {
      const nextWidth = Math.max(minWidth, Math.min(maxWidth, Math.round(clientX)));
      document.documentElement.style.setProperty("--sidebar-width", `${nextWidth}px`);
      localStorage.setItem("offline-file-namer-sidebar-width", String(nextWidth));
    };
    const beginResize = (event) => {
      dragging = true;
      handle.classList.add("resizing");
      if (event.pointerId !== undefined) handle.setPointerCapture(event.pointerId);
      document.body.style.userSelect = "none";
    };
    const moveResize = (event) => { if (dragging) setWidth(event.clientX); };
    const stopResize = () => {
      dragging = false;
      handle.classList.remove("resizing");
      document.body.style.userSelect = "";
    };
    handle.addEventListener("pointerdown", beginResize);
    handle.addEventListener("pointermove", moveResize);
    handle.addEventListener("pointerup", stopResize);
    handle.addEventListener("pointercancel", stopResize);
    handle.addEventListener("mousedown", beginResize);
    document.addEventListener("mousemove", moveResize);
    document.addEventListener("mouseup", stopResize);
  }

  function setupFileTableColumnResizers() {
    const table = $("fileTable");
    const headers = [...table.querySelectorAll("thead th[data-column-id]")];
    const columns = new Map([...table.querySelectorAll("col[data-column-id]")].map((column) => [column.dataset.columnId, column]));
    const widths = {};
    const minimumWidth = 44;
    let savedWidths = {};
    try {
      const saved = JSON.parse(localStorage.getItem(FILE_TABLE_COLUMN_STORAGE_KEY) || "{}");
      if (saved && typeof saved === "object" && !Array.isArray(saved)) savedWidths = saved;
    } catch (_error) {}

    const clampWidth = (value) => Math.max(minimumWidth, Math.min(1200, Math.round(Number(value) || minimumWidth)));
    const defaultWidths = Object.fromEntries(headers.map((header) => {
      const column = columns.get(header.dataset.columnId);
      return [header.dataset.columnId, clampWidth(column.dataset.defaultWidth)];
    }));
    const hasSavedWidths = headers.some((header) => {
      const value = Number(savedWidths[header.dataset.columnId]);
      return Number.isFinite(value) && value > 0;
    });
    if (!hasSavedWidths) {
      const flexibleColumns = ["current-name", "folder", "workflow", "parsed", "preview"];
      const defaultTotal = Object.values(defaultWidths).reduce((sum, width) => sum + width, 0);
      const extraWidth = Math.max(0, Math.floor(table.parentElement.clientWidth - defaultTotal));
      const extraPerColumn = Math.floor(extraWidth / flexibleColumns.length);
      const remainder = extraWidth % flexibleColumns.length;
      flexibleColumns.forEach((columnId, index) => {
        defaultWidths[columnId] += extraPerColumn + (index < remainder ? 1 : 0);
      });
    }
    const setTableWidth = () => {
      const total = Object.values(widths).reduce((sum, width) => sum + width, 0);
      table.style.width = `${total}px`;
      table.style.minWidth = `${total}px`;
    };
    const setColumnWidth = (header, value) => {
      const columnId = header.dataset.columnId;
      const width = clampWidth(value);
      widths[columnId] = width;
      columns.get(columnId).style.width = `${width}px`;
      const handle = header.querySelector(".column-resizer");
      if (handle) {
        handle.setAttribute("aria-valuenow", String(width));
        handle.setAttribute("aria-valuetext", `${width} 像素`);
      }
      setTableWidth();
    };
    const persistWidths = () => localStorage.setItem(FILE_TABLE_COLUMN_STORAGE_KEY, JSON.stringify(widths));

    headers.forEach((header) => {
      const column = columns.get(header.dataset.columnId);
      const defaultWidth = defaultWidths[header.dataset.columnId];
      const savedWidth = Number(savedWidths[header.dataset.columnId]);
      widths[header.dataset.columnId] = Number.isFinite(savedWidth) ? clampWidth(savedWidth) : defaultWidth;
      column.style.width = `${widths[header.dataset.columnId]}px`;

      const label = header.textContent.trim() || "选择";
      const handle = document.createElement("span");
      handle.className = "column-resizer";
      handle.tabIndex = 0;
      handle.setAttribute("role", "separator");
      handle.setAttribute("aria-orientation", "vertical");
      handle.setAttribute("aria-label", `调整${label}列宽`);
      handle.setAttribute("aria-valuemin", String(minimumWidth));
      handle.setAttribute("aria-valuemax", "1200");
      handle.setAttribute("aria-valuenow", String(widths[header.dataset.columnId]));
      handle.setAttribute("aria-valuetext", `${widths[header.dataset.columnId]} 像素`);
      handle.title = `拖动调整${label}列宽；双击恢复默认宽度`;
      header.appendChild(handle);

      handle.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
        event.preventDefault();
        const step = event.shiftKey ? 25 : 10;
        setColumnWidth(header, widths[header.dataset.columnId] + (event.key === "ArrowRight" ? step : -step));
        persistWidths();
      });
      handle.addEventListener("dblclick", (event) => {
        event.preventDefault();
        event.stopPropagation();
        setColumnWidth(header, defaultWidth);
        persistWidths();
      });
    });
    setTableWidth();

    let activeResize = null;
    const beginResize = (event) => {
      if (event.button !== undefined && event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const handle = event.currentTarget;
      const header = handle.closest("th[data-column-id]");
      activeResize = {
        header,
        startX: event.clientX,
        startWidth: widths[header.dataset.columnId],
      };
      header.classList.add("is-resizing");
      table.classList.add("is-resizing");
      document.body.style.userSelect = "none";
      if (event.pointerId !== undefined && handle.setPointerCapture) handle.setPointerCapture(event.pointerId);
    };
    const moveResize = (event) => {
      if (!activeResize) return;
      setColumnWidth(activeResize.header, activeResize.startWidth + event.clientX - activeResize.startX);
    };
    const stopResize = () => {
      if (!activeResize) return;
      activeResize.header.classList.remove("is-resizing");
      table.classList.remove("is-resizing");
      document.body.style.userSelect = "";
      activeResize = null;
      persistWidths();
    };
    headers.forEach((header) => {
      const handle = header.querySelector(".column-resizer");
      if (window.PointerEvent) handle.addEventListener("pointerdown", beginResize);
      else handle.addEventListener("mousedown", beginResize);
    });
    if (window.PointerEvent) {
      document.addEventListener("pointermove", moveResize);
      document.addEventListener("pointerup", stopResize);
      document.addEventListener("pointercancel", stopResize);
    } else {
      document.addEventListener("mousemove", moveResize);
      document.addEventListener("mouseup", stopResize);
    }
  }

  async function boot() {
    startClientLifecycle();
    startWorkflowTagSync();
    bindEvents();
    try { const payload = await api("/api/state"); applyState(payload.state); }
    catch (error) { showToast(error.message, true); }
  }

  boot();
})();
