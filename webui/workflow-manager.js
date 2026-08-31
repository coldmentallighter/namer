(() => {
  "use strict";

  window.name = "workflow-manager";

  const $ = (id) => document.getElementById(id);
  const state = {
    workflows: [],
    currentWorkflow: "",
    revision: 0,
    search: "",
    kind: "all",
    status: "all",
    selectedId: "",
    pendingFile: null,
    inspection: null,
    pendingAction: null, // { workflow, action: "disable" | "uninstall" | "purge" }
  };
  let toastTimer;

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
  const kindLabels = { resource: "软件内置", module: "外部安装", config: "配置型" };
  const trustLabels = { "no-code": "无代码", trusted: "已信任", changed: "代码已变化" };

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
        if (!nextRevision || nextRevision === state.revision) return;
        const hadRevision = state.revision > 0;
        if (!hadRevision) state.revision = nextRevision;
        else loadManage().catch((error) => showToast(error.message, true));
      } catch (_error) {}
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

  function postJson(path, payload) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      cache: "no-store",
    }).then(async (response) => {
      const body = await response.json();
      if (!response.ok || body.ok === false) throw new Error(body.error || "请求失败");
      return body;
    });
  }

  async function loadManage() {
    const payload = await postJson("/api/workflows/manage", {});
    state.workflows = payload.workflows || [];
    state.currentWorkflow = payload.current_workflow || "";
    state.revision = Number(payload.revision || state.revision || 0);
    if (state.selectedId && !state.workflows.some((entry) => entry.workflow_id === state.selectedId)) {
      state.selectedId = "";
    }
    render();
  }

  function visibleWorkflows() {
    const search = state.search.trim().toLowerCase();
    return state.workflows.filter((entry) => {
      const text = `${entry.name} ${entry.workflow_id}`.toLowerCase();
      return (!search || text.includes(search))
        && (state.kind === "all" || entry.kind === state.kind)
        && (state.status === "all"
            || (state.status === "enabled" && entry.enabled)
            || (state.status === "disabled" && !entry.enabled)
            || (state.status === "failed" && entry.diagnostics.length > 0));
    });
  }

  function entryById(workflowId) {
    return state.workflows.find((entry) => entry.workflow_id === workflowId);
  }

  function capabilityText(entry) {
    if (entry.kind !== "module") return "-";
    const caps = entry.capabilities || {};
    const parts = [];
    if (caps.providers?.length) parts.push(`metadata ${caps.providers.length}`);
    if (caps.filename_parsers?.length) parts.push(`解析 ${caps.filename_parsers.length}`);
    if (caps.normalizers?.length) parts.push(`规范化 ${caps.normalizers.length}`);
    if (caps.runners?.length) parts.push(`runner ${caps.runners.length}`);
    return parts.length ? parts.join(" · ") : "模块";
  }

  function render() {
    const visible = visibleWorkflows();
    const enabled = state.workflows.filter((entry) => entry.enabled).length;
    const modules = state.workflows.filter((entry) => entry.kind === "module").length;
    const failed = state.workflows.filter((entry) => entry.diagnostics.length > 0).length;
    $("metricTotal").textContent = String(state.workflows.length);
    $("metricEnabled").textContent = String(enabled);
    $("metricModule").textContent = String(modules);
    $("metricFailed").textContent = String(failed);
    $("listMeta").textContent = `${visible.length} / ${state.workflows.length} 个工作流 · 当前：${esc(state.currentWorkflow)}`;
    if (!visible.length) {
      $("workflowRows").innerHTML = `<tr><td colspan="9"><div class="manager-empty"><div><strong>没有符合条件的工作流</strong><span>调整筛选条件，或点击“导入工作流”安装一个。</span></div></div></td></tr>`;
    } else {
      $("workflowRows").innerHTML = visible.map((entry, index) => {
        const badges = [];
        if (entry.current) badges.push(`<span class="wf-badge current">使用中</span>`);
        badges.push(`<span class="wf-badge ${entry.enabled ? "" : "disabled"}">${entry.enabled ? "已启用" : "已停用"}</span>`);
        if (entry.diagnostics.length) badges.push(`<span class="wf-badge failed">加载失败</span>`);
        if (entry.trust === "changed") badges.push(`<span class="wf-badge changed">代码已变化</span>`);
        const actions = [];
        actions.push(`<button class="table-action" type="button" data-action="${entry.enabled ? "disable" : "enable"}">${entry.enabled ? "停用" : "启用"}</button>`);
        actions.push(`<button class="table-action" type="button" data-action="export">导出</button>`);
        if (entry.kind === "module") actions.push(`<button class="table-action danger" type="button" data-action="uninstall">卸载</button>`);
        if (entry.kind === "config") actions.push(`<button class="table-action danger" type="button" data-action="delete-config">删除</button>`);
        return `<tr data-workflow-id="${esc(entry.workflow_id)}" class="${entry.workflow_id === state.selectedId ? "selected" : ""}">
          <td class="manager-index-col">${index + 1}</td>
          <td><span class="tag-title">${esc(entry.name)}</span><span class="tag-subline">${esc(entry.workflow_id)}</span></td>
          <td>${esc(entry.version || "-")}</td>
          <td>${badges.join("")}</td>
          <td><span class="tag-source">${esc(kindLabels[entry.kind] || entry.kind)}</span></td>
          <td><span class="tag-status${entry.trust === "changed" ? " changed" : ""}">${esc(trustLabels[entry.trust] || entry.trust)}</span></td>
          <td><span class="wf-capability">${esc(capabilityText(entry))}</span></td>
          <td><span class="wf-diagnostics" title="${esc(entry.diagnostics.join("；"))}">${entry.diagnostics.length ? esc(entry.diagnostics[0]) : "-"}</span></td>
          <td class="workflow-action-col">${actions.join("")}</td>
        </tr>`;
      }).join("");
    }
    renderInspector();
  }

  function renderInspector() {
    const entry = entryById(state.selectedId);
    if (!entry) {
      $("workflowInspector").innerHTML = "选择一行查看详情";
      return;
    }
    const caps = entry.capabilities || {};
    const list = (label, values) => `<dt>${label}</dt><dd>${values?.length ? esc(values.join("、")) : "-"}</dd>`;
    $("workflowInspector").innerHTML = `<div class="inspector-name">${esc(entry.name)}</div>
      <div class="inspector-value">${esc(entry.workflow_id)}</div><div class="inspector-rule"></div>
      <dl class="inspector-list">
        <dt>来源</dt><dd>${esc(kindLabels[entry.kind] || entry.kind)}</dd>
        <dt>状态</dt><dd>${entry.enabled ? "已启用" : "已停用"}${entry.current ? "（当前使用）" : ""}</dd>
        <dt>版本</dt><dd>${esc(entry.version || "-")}</dd>
        <dt>字段</dt><dd>${entry.field_count} 个 · 模块 ${entry.module_count} 个</dd>
        <dt>信任</dt><dd>${esc(trustLabels[entry.trust] || entry.trust)}</dd>
        ${list("Metadata 提供者", caps.providers)}
        ${list("文件名解析器", caps.filename_parsers)}
        ${list("规范化器", caps.normalizers)}
        ${list("Runner", caps.runners)}
        <dt>安装目录</dt><dd>${esc(entry.source_dir || "-")}</dd>
        <dt>安装时间</dt><dd>${esc(entry.installed_at || "-")}</dd>
        <dt>安装 ID</dt><dd>${esc(entry.installation_id || "-")}</dd>
        <dt>包哈希</dt><dd>${esc(entry.package_sha256 ? entry.package_sha256.slice(0, 16) + "…" : "-")}</dd>
        <dt>代码哈希</dt><dd>${esc(entry.code_sha256 ? entry.code_sha256.slice(0, 16) + "…" : "-")}</dd>
      </dl>
      ${entry.diagnostics.length ? `<div class="inspector-diagnostics"><strong>加载诊断</strong><p>${esc(entry.diagnostics.join("；"))}</p></div>` : ""}
      <div class="inspector-actions">
        ${entry.current ? "" : `<button class="button button-outline full-width" data-inspector-action="${entry.enabled ? "disable" : "enable"}">${entry.enabled ? "停用" : "启用"}</button>`}
        <button class="button button-outline full-width" data-inspector-action="export">导出 .ffnf-workflow</button>
        ${entry.kind === "module" && !entry.current ? `<button class="button button-outline full-width danger-outline" data-inspector-action="uninstall">卸载</button>` : ""}
        ${entry.kind === "config" ? `<button class="button button-outline full-width danger-outline" data-inspector-action="delete-config">删除配置</button>` : ""}
        ${entry.kind === "config" ? `<button class="button button-outline full-width danger-outline" data-inspector-action="purge">清除数据</button>` : ""}
      </div>`;
  }

  function openSwitchModal(entry, action) {
    state.pendingAction = { workflow: entry, action };
    $("switchName").textContent = entry.name;
    $("switchSelect").innerHTML = state.workflows
      .filter((candidate) => candidate.workflow_id !== entry.workflow_id && candidate.enabled && !candidate.diagnostics.length)
      .map((candidate) => `<option value="${esc(candidate.workflow_id)}">${esc(candidate.name)}</option>`).join("")
      || `<option value="">没有其他可用工作流</option>`;
    $("switchModal").classList.remove("hidden");
    window.setTimeout(() => $("switchSelect").focus(), 0);
  }

  function closeSwitchModal() {
    $("switchModal").classList.add("hidden");
    state.pendingAction = null;
  }

  function openDisableModal(entry) {
    state.pendingAction = { workflow: entry, action: "disable" };
    $("disableName").textContent = entry.name;
    $("disableModal").classList.remove("hidden");
    window.setTimeout(() => $("confirmDisable").focus(), 0);
  }

  function closeDisableModal() {
    $("disableModal").classList.add("hidden");
    state.pendingAction = null;
  }

  function openUninstallModal(entry) {
    state.pendingAction = { workflow: entry, action: "uninstall" };
    $("uninstallName").textContent = entry.name;
    $("uninstallModal").classList.remove("hidden");
    window.setTimeout(() => $("confirmUninstall").focus(), 0);
  }

  function closeUninstallModal() {
    $("uninstallModal").classList.add("hidden");
    state.pendingAction = null;
  }

  function openPurgeModal(entry) {
    state.pendingAction = { workflow: entry, action: "purge" };
    $("purgeName").textContent = entry.name;
    $("purgeModal").classList.remove("hidden");
    window.setTimeout(() => $("confirmPurge").focus(), 0);
  }

  function closePurgeModal() {
    $("purgeModal").classList.add("hidden");
    state.pendingAction = null;
  }

  function openDeleteConfigModal(entry) {
    state.pendingAction = { workflow: entry, action: "delete-config" };
    $("deleteConfigName").textContent = entry.name;
    $("deleteConfigModal").classList.remove("hidden");
    window.setTimeout(() => $("confirmDeleteConfig").focus(), 0);
  }

  function closeDeleteConfigModal() {
    $("deleteConfigModal").classList.add("hidden");
    state.pendingAction = null;
  }

  async function runPendingAction() {
    const pending = state.pendingAction;
    if (!pending) return;
    const { workflow, action } = pending;
    try {
      if (action === "disable") {
        const result = await postJson("/api/workflow/enable", { workflow_id: workflow.workflow_id, enabled: false });
        state.workflows = result.workflows || state.workflows;
        showToast(`已停用“${workflow.name}”`);
      } else if (action === "enable") {
        const result = await postJson("/api/workflow/enable", { workflow_id: workflow.workflow_id, enabled: true });
        state.workflows = result.workflows || state.workflows;
        showToast(`已启用“${workflow.name}”`);
      } else if (action === "uninstall") {
        const result = await postJson("/api/workflow/uninstall", { installation_id: workflow.installation_id });
        state.workflows = result.workflows || state.workflows;
        showToast(`已卸载“${workflow.name}”`);
      } else if (action === "purge") {
        await postJson("/api/workflow/purge-data", { workflow_id: workflow.workflow_id });
        showToast(`已清除“${workflow.name}”的数据`);
      } else if (action === "delete-config") {
        const result = await postJson("/api/workflow/delete-config", { workflow_id: workflow.workflow_id });
        state.workflows = result.workflows || state.workflows;
        showToast(`已删除配置“${workflow.name}”`);
      }
    } catch (error) {
      showToast(error.message, true);
      return;
    }
    closeUninstallModal();
    closePurgeModal();
    closeDisableModal();
    closeDeleteConfigModal();
    closeSwitchModal();
    render();
  }

  function triggerAction(workflowId, action) {
    const entry = entryById(workflowId);
    if (!entry) return;
    if (action === "export") {
      window.open(`/api/workflow-export?workflow_id=${encodeURIComponent(workflowId)}`, "_blank");
      return;
    }
    if (action === "uninstall") {
      if (entry.current) openSwitchModal(entry, "uninstall");
      else openUninstallModal(entry);
      return;
    }
    if (action === "purge") {
      openPurgeModal(entry);
      return;
    }
    if (action === "delete-config") {
      openDeleteConfigModal(entry);
      return;
    }
    if (action === "disable") {
      if (entry.current) openSwitchModal(entry, "disable");
      else openDisableModal(entry);
      return;
    }
    if (action === "enable") {
      state.pendingAction = { workflow: entry, action: "enable" };
      runPendingAction();
    }
  }

  async function importWorkflow(file) {
    if (!file) return;
    state.pendingFile = file;
    $("importSummary").classList.add("hidden");
    $("importConflictOptions").style.display = "none";
    $("importTrust").checked = false;
    $("confirmImport").disabled = true;
    try {
      const form = new FormData();
      form.append("file", file);
      const response = await fetch("/api/workflow/inspect", { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "预检失败");
      const inspection = payload.inspection;
      state.inspection = inspection;
      $("importTitle").textContent = `导入：${inspection.name}`;
      $("importTrust").parentElement.style.display = inspection.has_modules ? "" : "none";
      const rows = [
        ["工作流 ID", inspection.workflow_id],
        ["版本", inspection.version],
        ["字段", `${inspection.field_count} 个`],
        ["Python 模块", inspection.has_modules ? `${inspection.module_count} 个` : "无"],
        ["包哈希", inspection.sha256.slice(0, 16) + "…"],
        ["文件", inspection.module_files.join("、") || "仅 workflow.json"],
      ];
      if (inspection.manifest_error) rows.push(["模块清单", inspection.manifest_error]);
      $("importSummary").innerHTML = `<dl class="inspector-list">${rows.map(([label, value]) => `<dt>${label}</dt><dd>${esc(value)}</dd>`).join("")}</dl>`;
      $("importSummary").classList.remove("hidden");
      const exists = !!inspection.exists;
      $("importConflictOptions").style.display = exists ? "" : "none";
      $("confirmImport").disabled = inspection.has_modules; // requires explicit trust
      $("importHint").textContent = inspection.has_modules
        ? "此包包含 Python 模块，勾选信任后才能安装。"
        : "确认内容无误后点击安装。";
      $("importModal").classList.remove("hidden");
    } catch (error) {
      showToast(error.message, true);
    }
  }

  function closeImportModal() {
    $("importModal").classList.add("hidden");
    state.pendingFile = null;
    state.inspection = null;
  }

  async function confirmImport() {
    const inspection = state.inspection;
    const file = state.pendingFile;
    if (!inspection || !file) return;
    if (inspection.has_modules && !$("importTrust").checked) {
      return showToast("请先确认信任此包中的 Python 模块", true);
    }
    const strategy = document.querySelector('input[name="importStrategy"]:checked')?.value || "copy";
    const confirmButton = $("confirmImport");
    confirmButton.disabled = true;
    try {
      const form = new FormData();
      form.append("file", file);
      form.append("strategy", strategy);
      form.append("trust_modules", $("importTrust").checked ? "true" : "false");
      const response = await fetch("/api/workflow/import", { method: "POST", body: form });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) throw new Error(payload.error || "导入失败");
      closeImportModal();
      await loadManage();
      const note = payload.copied ? "（已作为副本）" : payload.replaced ? "（已替换）" : "";
      showToast(`已导入工作流：${payload.imported.name}${note}`);
    } catch (error) {
      showToast(error.message, true);
    } finally {
      confirmButton.disabled = false;
    }
  }

  function bindEvents() {
    applyTheme(localStorage.getItem("offline-file-namer-theme") || "light");
    $("themeToggle").addEventListener("click", () => applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
    $("workflowSearch").addEventListener("input", (event) => { state.search = event.target.value; render(); });
    $("kindFilter").addEventListener("change", (event) => { state.kind = event.target.value; render(); });
    $("stateFilter").addEventListener("change", (event) => { state.status = event.target.value; render(); });
    $("refreshList").addEventListener("click", () => loadManage().catch((error) => showToast(error.message, true)));
    $("importWorkflow").addEventListener("click", () => $("workflowFile").click());
    $("workflowFile").addEventListener("change", (event) => {
      importWorkflow(event.target.files[0]);
      event.target.value = "";
    });
    $("workflowRows").addEventListener("click", (event) => {
      const row = event.target.closest("tr[data-workflow-id]");
      if (!row) return;
      const workflowId = row.dataset.workflowId;
      if (event.target.dataset.action) {
        triggerAction(workflowId, event.target.dataset.action);
        return;
      }
      state.selectedId = workflowId;
      render();
    });
    $("workflowInspector").addEventListener("click", (event) => {
      const action = event.target.dataset.inspectorAction;
      if (!action) return;
      triggerAction(state.selectedId, action);
    });
    $("confirmImport").addEventListener("click", confirmImport);
    $("closeImport").addEventListener("click", closeImportModal);
    $("cancelImport").addEventListener("click", closeImportModal);
    $("importModal").addEventListener("click", (event) => { if (event.target === $("importModal")) closeImportModal(); });
    $("importTrust").addEventListener("change", () => { $("confirmImport").disabled = !$("importTrust").checked; });
    $("confirmSwitch").addEventListener("click", async () => {
      const pending = state.pendingAction;
      const target = $("switchSelect").value;
      if (!pending || !target) return showToast("没有可切换的工作流", true);
      try {
        await postJson("/api/workflow/select", { workflow_id: target });
        closeSwitchModal();
        if (pending.action === "uninstall") openUninstallModal(pending.workflow);
        else openDisableModal(pending.workflow);
      } catch (error) {
        showToast(error.message, true);
      }
    });
    $("closeSwitch").addEventListener("click", closeSwitchModal);
    $("cancelSwitch").addEventListener("click", closeSwitchModal);
    $("switchModal").addEventListener("click", (event) => { if (event.target === $("switchModal")) closeSwitchModal(); });
    $("confirmDisable").addEventListener("click", () => runPendingAction());
    $("closeDisable").addEventListener("click", closeDisableModal);
    $("cancelDisable").addEventListener("click", closeDisableModal);
    $("disableModal").addEventListener("click", (event) => { if (event.target === $("disableModal")) closeDisableModal(); });
    $("confirmUninstall").addEventListener("click", () => runPendingAction());
    $("closeUninstall").addEventListener("click", closeUninstallModal);
    $("cancelUninstall").addEventListener("click", closeUninstallModal);
    $("uninstallModal").addEventListener("click", (event) => { if (event.target === $("uninstallModal")) closeUninstallModal(); });
    $("confirmPurge").addEventListener("click", () => runPendingAction());
    $("closePurge").addEventListener("click", closePurgeModal);
    $("cancelPurge").addEventListener("click", closePurgeModal);
    $("purgeModal").addEventListener("click", (event) => { if (event.target === $("purgeModal")) closePurgeModal(); });
    $("confirmDeleteConfig").addEventListener("click", () => runPendingAction());
    $("closeDeleteConfig").addEventListener("click", closeDeleteConfigModal);
    $("cancelDeleteConfig").addEventListener("click", closeDeleteConfigModal);
    $("deleteConfigModal").addEventListener("click", (event) => { if (event.target === $("deleteConfigModal")) closeDeleteConfigModal(); });
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Escape") return;
      if (!$("importModal").classList.contains("hidden")) closeImportModal();
      else if (!$("switchModal").classList.contains("hidden")) closeSwitchModal();
      else if (!$("disableModal").classList.contains("hidden")) closeDisableModal();
      else if (!$("uninstallModal").classList.contains("hidden")) closeUninstallModal();
      else if (!$("purgeModal").classList.contains("hidden")) closePurgeModal();
      else if (!$("deleteConfigModal").classList.contains("hidden")) closeDeleteConfigModal();
    });
  }

  async function boot() {
    startClientLifecycle();
    bindEvents();
    try {
      await loadManage();
    } catch (error) {
      $("listMeta").textContent = error.message;
      showToast(error.message, true);
    }
  }

  boot();
})();
