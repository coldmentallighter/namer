"""Tkinter desktop UI for the offline file namer."""

from __future__ import annotations

import os
import tkinter as tk
import ctypes
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _TkBase = TkinterDnD.Tk
except ImportError:  # optional enhancement; folder picker always works
    DND_FILES = "DND_Files"
    _TkBase = tk.Tk

from namer_core import (
    ExcelMatchResult,
    FileRecord,
    NamingGroup,
    assign_numeric,
    compose_filename,
    execute_rename,
    export_filename_tables,
    import_xlsx,
    open_in_explorer,
    preview_group,
    scan_folder,
    undo_last,
    validate_filename,
    wav_duration,
)


class NamerApp(_TkBase):
    """A complete local workflow: scan, preview, rename, export, undo."""

    def __init__(self) -> None:
        super().__init__()
        self.title("离线批量文件命名器")
        self.geometry("1500x900")
        self.minsize(1100, 700)
        self.root_var = tk.StringVar()
        self.meta_var = tk.StringVar()
        self.separator_var = tk.StringVar(value="_")
        self.child_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="原文件名")
        self.start_var = tk.StringVar(value="1")
        self.width_var = tk.StringVar(value="2")
        self.step_var = tk.StringVar(value="1")
        self.search_var = tk.StringVar()
        self.status_filter_var = tk.StringVar(value="全部状态")
        self.include_hidden_var = tk.BooleanVar(value=False)
        self.include_system_var = tk.BooleanVar(value=False)
        self.export_hidden_var = tk.BooleanVar(value=False)
        self.export_system_var = tk.BooleanVar(value=False)
        self.current_group_key: str | None = None
        self.scan_result = None
        self.ext_vars: dict[str, tk.BooleanVar] = {}
        self.export_ext_vars: dict[str, tk.BooleanVar] = {}
        self.group_enabled: dict[str, bool] = {}
        self.tree_records: dict[str, FileRecord] = {}
        self.excel_mappings: dict[str, dict[str, str]] = {}
        self.excel_skipped: dict[str, set[str]] = {}
        self.audio_record: FileRecord | None = None
        self.audio_job = None
        self.audio_paused = False
        self.audio_alias = "offline_file_namer_audio"
        self.audio_duration = 0.0
        self.history_path = self._history_path()
        self._build_style()
        self._build_ui()

    @staticmethod
    def _history_path() -> Path:
        appdata = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        base = Path(appdata) if appdata else Path.home()
        return base / "OfflineFileNamer" / "history.json"

    def _build_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Segoe UI", 15, "bold"))
        style.configure("Treeview", rowheight=28)
        style.configure("Toolbar.TButton", padding=(8, 4))

    def _build_ui(self) -> None:
        header = ttk.Frame(self, padding=(12, 10))
        header.pack(fill="x")
        ttk.Label(header, text="离线批量文件命名器", style="Title.TLabel").pack(side="left", padx=(0, 16))
        ttk.Label(header, text="根目录").pack(side="left")
        root_entry = ttk.Entry(header, textvariable=self.root_var, width=70)
        root_entry.pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(header, text="选择文件夹", command=self.choose_root).pack(side="left", padx=3)
        ttk.Button(header, text="扫描", command=self.scan).pack(side="left", padx=3)
        ttk.Checkbutton(header, text="包含隐藏文件", variable=self.include_hidden_var).pack(side="left", padx=5)
        ttk.Checkbutton(header, text="包含系统文件", variable=self.include_system_var).pack(side="left", padx=5)
        self._enable_drop(root_entry)

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=10)
        rename_tab = ttk.Frame(self.notebook)
        export_tab = ttk.Frame(self.notebook)
        self.notebook.add(rename_tab, text="批量命名")
        self.notebook.add(export_tab, text="导出文件名表格")
        self._build_rename_tab(rename_tab)
        self._build_export_tab(export_tab)

        log_frame = ttk.LabelFrame(self, text="日志")
        log_frame.pack(fill="x", padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frame, height=7, state="disabled", wrap="none")
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        log_scroll.pack(side="right", fill="y")
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.tag_configure("INFO", foreground="#1f4e79")
        self.log_text.tag_configure("WARN", foreground="#a15c00")
        self.log_text.tag_configure("ERROR", foreground="#b00020")
        self.log("INFO", "应用已启动，所有操作均在本机完成。")

    def _enable_drop(self, widget: tk.Widget) -> None:
        """Use tkinterdnd2 when installed; normal Tk remains fully usable."""
        try:
            widget.drop_target_register(DND_FILES)  # type: ignore[attr-defined]
            widget.dnd_bind("<<Drop>>", self._on_drop)  # type: ignore[attr-defined]
        except (AttributeError, tk.TclError):
            widget.bind("<<Drop>>", self._on_drop)

    def _on_drop(self, event) -> None:
        data = getattr(event, "data", "").strip().strip("{}")
        if data:
            first = data.split("} {")[0].strip("{}")
            path = Path(first)
            if path.is_dir():
                self.root_var.set(str(path))
                self.scan()

    def _build_rename_tab(self, parent: ttk.Frame) -> None:
        top = ttk.Frame(parent, padding=8)
        top.pack(fill="x")
        self.ext_frame = ttk.LabelFrame(top, text="扫描到的扩展名（参与处理）")
        self.ext_frame.pack(side="left", fill="x", expand=True)
        self._add_ext_placeholder(self.ext_frame)
        ttk.Button(top, text="导入 XLSX", command=self.import_excel, style="Toolbar.TButton").pack(side="left", padx=5)
        ttk.Button(top, text="预览目标文件名", command=self.apply_preview, style="Toolbar.TButton").pack(side="left", padx=5)
        ttk.Button(top, text="重命名当前组", command=self.rename_current, style="Toolbar.TButton").pack(side="left", padx=5)
        ttk.Button(top, text="执行全部已选组", command=self.rename_all, style="Toolbar.TButton").pack(side="left", padx=5)
        ttk.Button(top, text="撤销最近操作", command=self.undo, style="Toolbar.TButton").pack(side="left", padx=5)

        body = ttk.PanedWindow(parent, orient="horizontal")
        body.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        group_panel = ttk.Labelframe(body, text="命名组", padding=6)
        body.add(group_panel, weight=0)
        self.group_list = tk.Listbox(group_panel, width=31, exportselection=False, activestyle="none")
        self.group_list.pack(fill="both", expand=True)
        self.group_list.bind("<<ListboxSelect>>", self._on_group_select)
        ttk.Button(group_panel, text="切换当前组执行状态", command=self.toggle_current_group).pack(fill="x", pady=(6, 0))
        self.group_state_label = ttk.Label(group_panel, text="暂无扫描结果", wraplength=210)
        self.group_state_label.pack(fill="x", pady=(6, 0))

        right = ttk.Frame(body)
        body.add(right, weight=1)
        settings = ttk.LabelFrame(right, text="命名设置", padding=7)
        settings.pack(fill="x")
        self.group_prefix_var = tk.StringVar()
        self._setting_row(settings, 0, "元前缀", self.meta_var, 55)
        self._setting_row(settings, 1, "组前缀", self.group_prefix_var, 55)
        self._setting_row(settings, 2, "子前缀", self.child_var, 55)
        self._setting_row(settings, 3, "分隔符", self.separator_var, 12)
        ttk.Label(settings, text="名称模式").grid(row=0, column=4, sticky="e", padx=(18, 4))
        mode = ttk.Combobox(settings, textvariable=self.mode_var, state="readonly", width=12,
                            values=("原文件名", "数字编号", "Excel 名称"))
        mode.grid(row=0, column=5, sticky="w")
        mode.bind("<<ComboboxSelected>>", lambda _e: self.apply_preview())
        ttk.Label(settings, text="起始").grid(row=1, column=4, sticky="e", padx=(18, 4))
        ttk.Entry(settings, textvariable=self.start_var, width=7).grid(row=1, column=5, sticky="w")
        ttk.Label(settings, text="位数").grid(row=2, column=4, sticky="e", padx=(18, 4))
        ttk.Entry(settings, textvariable=self.width_var, width=7).grid(row=2, column=5, sticky="w")
        ttk.Label(settings, text="步长").grid(row=3, column=4, sticky="e", padx=(18, 4))
        ttk.Entry(settings, textvariable=self.step_var, width=7).grid(row=3, column=5, sticky="w")
        self.execute_group_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(settings, text="当前组加入‘全部执行’", variable=self.execute_group_var,
                        command=self._sync_current_group_state).grid(row=3, column=6, padx=(12, 0), sticky="w")
        for col in range(7):
            settings.columnconfigure(col, weight=1 if col in (1, 3) else 0)

        filter_bar = ttk.Frame(right, padding=(0, 7, 0, 5))
        filter_bar.pack(fill="x")
        ttk.Label(filter_bar, text="搜索文件名").pack(side="left")
        ttk.Entry(filter_bar, textvariable=self.search_var, width=28).pack(side="left", padx=5)
        self.search_var.trace_add("write", lambda *_: self.refresh_tree())
        ttk.Label(filter_bar, text="状态").pack(side="left", padx=(16, 3))
        status_box = ttk.Combobox(filter_bar, state="readonly", textvariable=self.status_filter_var, width=13,
                                  values=("全部状态", "Ready", "Renamed", "Conflict", "Skipped", "Error", "未匹配", "Unchanged"))
        status_box.pack(side="left")
        status_box.bind("<<ComboboxSelected>>", lambda _e: self.refresh_tree())
        ttk.Button(filter_bar, text="全选可见", command=self.select_visible).pack(side="left", padx=(16, 3))
        ttk.Button(filter_bar, text="取消选择", command=self.deselect_all).pack(side="left", padx=3)
        ttk.Button(filter_bar, text="移除选中行", command=self.remove_selected).pack(side="left", padx=3)

        table_frame = ttk.Frame(right)
        table_frame.pack(fill="both", expand=True)
        columns = ("select", "source", "folder", "ext", "child", "name", "preview", "status", "play", "rename", "remove")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {"select": "选择", "source": "当前文件名", "folder": "当前文件夹/相对路径", "ext": "扩展名",
                    "child": "子前缀", "name": "名称", "preview": "新文件名预览", "status": "状态",
                    "play": "播放", "rename": "单项重命名", "remove": "移除"}
        widths = {"select": 44, "source": 180, "folder": 190, "ext": 70, "child": 100, "name": 140,
                  "preview": 300, "status": 110, "play": 60, "rename": 90, "remove": 60}
        for column in columns:
            self.tree.heading(column, text=headings[column])
            self.tree.column(column, width=widths[column], anchor="w", stretch=column in {"source", "folder", "preview"})
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        self.tree.bind("<ButtonPress-1>", self._tree_press)
        self.tree.bind("<ButtonRelease-1>", self._tree_release)
        self.tree.bind("<Double-1>", self._tree_double_click)

        audio = ttk.LabelFrame(right, text="WAV 预览", padding=5)
        audio.pack(fill="x", pady=(7, 0))
        self.audio_label = ttk.Label(audio, text="未选择 WAV 文件")
        self.audio_label.pack(side="left", padx=(0, 10))
        ttk.Button(audio, text="播放", command=self.play_selected).pack(side="left", padx=2)
        self.audio_pause_btn = ttk.Button(audio, text="暂停", command=self.pause_audio)
        self.audio_pause_btn.pack(side="left", padx=2)
        ttk.Button(audio, text="停止", command=self.stop_audio).pack(side="left", padx=2)
        self.audio_progress = ttk.Scale(audio, from_=0, to=1, orient="horizontal", length=180)
        self.audio_progress.pack(side="left", padx=10)
        ttk.Label(audio, text="音量").pack(side="left")
        self.volume_scale = ttk.Scale(audio, from_=0, to=100, orient="horizontal", length=100)
        self.volume_scale.set(80)
        self.volume_scale.pack(side="left", padx=5)
        self.volume_scale.bind("<ButtonRelease-1>", lambda _e: self._set_audio_volume())
        self.audio_time = ttk.Label(audio, text="00:00 / 00:00")
        self.audio_time.pack(side="left", padx=5)

    def _setting_row(self, parent, row: int, label: str, variable: tk.StringVar, width: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="e", padx=(0, 5), pady=2)
        entry = ttk.Entry(parent, textvariable=variable, width=width)
        entry.grid(row=row, column=1, columnspan=3, sticky="ew", pady=2)
        entry.bind("<KeyRelease>", lambda _e: self.apply_preview())

    def _build_export_tab(self, parent: ttk.Frame) -> None:
        controls = ttk.Frame(parent, padding=12)
        controls.pack(fill="x")
        ttk.Label(controls, text="导出根目录").pack(side="left")
        ttk.Entry(controls, textvariable=self.root_var, width=75).pack(side="left", padx=6, fill="x", expand=True)
        ttk.Button(controls, text="选择文件夹", command=self.choose_root).pack(side="left", padx=3)
        ttk.Button(controls, text="扫描并刷新扩展名", command=lambda: self.scan(for_export=True)).pack(side="left", padx=3)
        ttk.Checkbutton(controls, text="包含隐藏文件", variable=self.export_hidden_var).pack(side="left", padx=5)
        ttk.Checkbutton(controls, text="包含系统文件", variable=self.export_system_var).pack(side="left", padx=5)
        self.export_ext_frame = ttk.LabelFrame(parent, text="导出扩展名")
        self.export_ext_frame.pack(fill="x", padx=12, pady=(0, 12))
        self._add_ext_placeholder(self.export_ext_frame)
        action = ttk.Frame(parent, padding=(12, 0))
        action.pack(fill="x")
        ttk.Button(action, text="导出 XLSX 并打开根目录", command=self.export_tables, style="Toolbar.TButton").pack(side="left")
        self.export_result = ttk.Label(action, text="尚未导出")
        self.export_result.pack(side="left", padx=15)
        ttk.Label(parent, text="每个直接包含所选扩展名文件的文件夹生成独立工作簿；输出到该文件夹父级，不覆盖已有文件。",
                  wraplength=1000).pack(anchor="w", padx=12, pady=12)

    def _add_ext_placeholder(self, frame) -> None:
        ttk.Label(frame, text="请先选择根目录并扫描").pack(side="left", padx=8, pady=5)

    def choose_root(self) -> None:
        path = filedialog.askdirectory(title="选择根目录")
        if path:
            self.root_var.set(path)
            self.scan()

    def scan(self, for_export: bool = False) -> None:
        root = self.root_var.get().strip()
        if not root:
            messagebox.showwarning("需要根目录", "请选择或拖入一个文件夹。")
            return
        try:
            result = scan_folder(root,
                                 self.export_hidden_var.get() if for_export else self.include_hidden_var.get(),
                                 self.export_system_var.get() if for_export else self.include_system_var.get())
        except Exception as exc:
            self.log("ERROR", f"扫描失败: {exc}")
            messagebox.showerror("扫描失败", str(exc))
            return
        if for_export:
            self.export_scan_result = result
            self._refresh_export_ext_controls(result)
            self.export_result.configure(text=f"可导出 {len(result.records)} 个文件")
            self.log("INFO", f"导出扫描完成：{len(result.records)} 个文件，{len(result.extension_counts)} 种扩展名。")
            return
        self.scan_result = result
        self.meta_var.set(Path(self.scan_result.root).name)
        self.groups = self.scan_result.groups
        self.group_enabled = {key: True for key in self.groups}
        self._refresh_ext_controls()
        self._refresh_group_list()
        if self.groups:
            self.group_list.selection_set(0)
            self._select_group_index(0)
        self.log("INFO", f"扫描完成：{len(self.scan_result.records)} 个文件，{len(self.groups)} 个命名组，{len(self.scan_result.extension_counts)} 种扩展名。")
        if self.scan_result.skipped:
            self.log("INFO", f"已忽略 {len(self.scan_result.skipped)} 个生成的表格或不可读条目。")
        self.export_result.configure(text=f"可导出 {len(self.scan_result.records)} 个文件")

    def _refresh_export_ext_controls(self, result) -> None:
        for child in self.export_ext_frame.winfo_children():
            child.destroy()
        self.export_ext_vars.clear()
        for ext, count in result.extension_counts.items():
            var = tk.BooleanVar(value=True)
            self.export_ext_vars[ext] = var
            ttk.Checkbutton(self.export_ext_frame, text=f"{ext.lstrip('.').upper() or '无扩展名'} ({count})",
                            variable=var).pack(side="left", padx=7, pady=5)

    def _refresh_ext_controls(self) -> None:
        for frame, target in ((self.ext_frame, self.ext_vars), (self.export_ext_frame, self.export_ext_vars)):
            for child in frame.winfo_children():
                child.destroy()
            target.clear()
            for ext, count in self.scan_result.extension_counts.items():
                var = tk.BooleanVar(value=True)
                target[ext] = var
                options = {"text": f"{ext.lstrip('.').upper() or '无扩展名'} ({count})", "variable": var}
                if frame is self.ext_frame:
                    options["command"] = self._extension_changed
                ttk.Checkbutton(frame, **options).pack(side="left", padx=7, pady=5)

    def _extension_changed(self) -> None:
        for record in getattr(self.scan_result, "records", []):
            record.selected = bool(self.ext_vars.get(record.extension, tk.BooleanVar(value=False)).get())
        self.refresh_tree()

    def _refresh_group_list(self) -> None:
        self.group_list.delete(0, "end")
        for key, group in self.groups.items():
            mark = "☑" if self.group_enabled.get(key, True) else "☐"
            self.group_list.insert("end", f"{mark} {group.label}")

    def _select_group_index(self, index: int) -> None:
        if not self.groups:
            return
        keys = list(self.groups)
        self.current_group_key = keys[index]
        group = self.groups[self.current_group_key]
        self.group_prefix_var.set(group.prefix)
        self.execute_group_var.set(self.group_enabled.get(group.key, True))
        self.refresh_tree()
        self.group_state_label.configure(text=f"当前组：{group.label}\n目录：{group.folder}")

    def _on_group_select(self, _event=None) -> None:
        selection = self.group_list.curselection()
        if selection:
            self._select_group_index(selection[0])

    def _sync_current_group_state(self) -> None:
        if self.current_group_key:
            self.group_enabled[self.current_group_key] = self.execute_group_var.get()
            self._refresh_group_list()
            self.group_list.selection_set(list(self.groups).index(self.current_group_key))

    def toggle_current_group(self) -> None:
        self.execute_group_var.set(not self.execute_group_var.get())
        self._sync_current_group_state()

    def current_group(self) -> NamingGroup | None:
        return self.groups.get(self.current_group_key) if self.current_group_key else None

    def apply_preview(self) -> None:
        group = self.current_group()
        if not group:
            return
        group.prefix = self.group_prefix_var.get()
        separator = self.separator_var.get()
        for record in group.records:
            record.child_prefix = self.child_var.get() if self.child_var.get() else record.child_prefix
            record.name = record.base_name
        mode = self.mode_var.get()
        if mode == "数字编号":
            try:
                start, width, step = int(self.start_var.get()), int(self.width_var.get()), int(self.step_var.get())
            except ValueError:
                self.log("WARN", "数字编号参数必须为整数")
                return
            assign_numeric(group, start, width, step, self.meta_var.get(), separator)
        elif mode == "Excel 名称":
            mapping = self.excel_mappings.get(group.key, {})
            skipped = self.excel_skipped.get(group.key, set())
            for record in group.records:
                if record.path in mapping:
                    record.name = mapping[record.path]
                    record.selected = True
                elif record.path in skipped:
                    record.status = "Skipped"
                    record.status_detail = "Excel B 列为空"
                    record.selected = False
                    record.target_name = compose_filename(self.meta_var.get(), group.prefix, record.child_prefix,
                                                          record.name, record.extension_original or record.extension, separator)
                    continue
                record.target_name = compose_filename(self.meta_var.get(), group.prefix, record.child_prefix,
                                                      record.name, record.extension_original or record.extension, separator)
                if record.path not in mapping and record.selected:
                    record.status = "未匹配"
                    record.status_detail = "Excel 未提供名称"
                    record.selected = False
        else:
            preview_group(group, self.meta_var.get(), separator)
        # Surface filename syntax problems before the user presses Execute.
        for record in group.records:
            if record.selected:
                syntax_error = validate_filename(record.target_name)
                if syntax_error:
                    record.status = "Conflict"
                    record.status_detail = syntax_error
                elif record.status == "Conflict":
                    record.status = "Ready"
                    record.status_detail = ""
        self.refresh_tree()

    def visible_records(self) -> list[FileRecord]:
        group = self.current_group()
        if not group:
            return []
        search = self.search_var.get().casefold().strip()
        status = self.status_filter_var.get()
        return [record for record in group.records
                if not record.removed
                and (not search or search in record.original_name.casefold() or search in record.target_name.casefold())
                and (status == "全部状态" or record.status == status)]

    def refresh_tree(self) -> None:
        if not hasattr(self, "tree"):
            return
        self.tree.delete(*self.tree.get_children())
        self.tree_records.clear()
        for index, record in enumerate(self.visible_records()):
            iid = f"row-{index}-{abs(hash(record.path))}"
            play = "▶" if record.extension.casefold() == ".wav" else ""
            values = ("☑" if record.selected else "☐", record.original_name, record.relative_folder,
                      record.extension, record.child_prefix, record.name, record.target_name,
                      record.status, play, "改名", "移除")
            self.tree.insert("", "end", iid=iid, values=values,
                             tags=("error" if record.status in {"Conflict", "Error", "未匹配"} else "",))
            self.tree_records[iid] = record
        self.tree.tag_configure("error", background="#ffe0e0")

    def _tree_press(self, event) -> None:
        self.drag_iid = self.tree.identify_row(event.y)
        self.drag_column = self.tree.identify_column(event.x)

    def _tree_release(self, event) -> None:
        source_iid = getattr(self, "drag_iid", "")
        row = self.tree.identify_row(event.y)
        if source_iid and row and row != source_iid and self.drag_column not in {"#1", "#9", "#10", "#11"}:
            source = self.tree_records.get(source_iid)
            target = self.tree_records.get(row)
            group = self.current_group()
            if source and target and group:
                group.records.remove(source)
                group.records.insert(group.records.index(target), source)
                self.log("INFO", f"已调整顺序：{source.original_name}")
                self.refresh_tree()
                return
        self._tree_click(event)

    def _tree_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        record = self.tree_records.get(row)
        if not record:
            return
        if column == "#1":
            record.selected = not record.selected
            self.refresh_tree()
        elif column == "#9" and record.extension.casefold() == ".wav":
            self.play_record(record)
        elif column == "#10":
            self.rename_one(record)
        elif column == "#11":
            record.selected = False
            record.removed = True
            record.status = "Skipped"
            record.status_detail = "Removed from this task"
            self.refresh_tree()

    def _tree_double_click(self, event) -> None:
        row = self.tree.identify_row(event.y)
        column = self.tree.identify_column(event.x)
        record = self.tree_records.get(row)
        if not record or column not in {"#5", "#6"}:
            return
        bbox = self.tree.bbox(row, column)
        if not bbox:
            return
        variable = tk.StringVar(value=record.child_prefix if column == "#5" else record.name)
        entry = ttk.Entry(self.tree, textvariable=variable)
        entry.place(x=bbox[0], y=bbox[1], width=bbox[2], height=bbox[3])
        entry.focus_set()
        def finish(_event=None):
            if column == "#5":
                record.child_prefix = variable.get()
            else:
                record.base_name = variable.get()
                record.name = record.base_name
            entry.destroy()
            self.apply_preview()
        entry.bind("<Return>", finish)
        entry.bind("<Escape>", lambda _e: entry.destroy())
        entry.bind("<FocusOut>", finish)

    def select_visible(self) -> None:
        for record in self.visible_records():
            record.selected = True
        self.refresh_tree()

    def deselect_all(self) -> None:
        for record in self.visible_records():
            record.selected = False
        self.refresh_tree()

    def remove_selected(self) -> None:
        removed = 0
        for record in self.visible_records():
            if record.selected:
                record.selected = False
                record.removed = True
                record.status = "Skipped"
                record.status_detail = "Removed from this task"
                removed += 1
        if removed:
            self.log("INFO", f"已从本次任务移除 {removed} 个文件（未删除磁盘文件）。")
        self.refresh_tree()

    def import_excel(self) -> None:
        group = self.current_group()
        if not group:
            messagebox.showwarning("没有命名组", "请先扫描并选择命名组。")
            return
        path = filedialog.askopenfilename(title="选择 XLSX", filetypes=[("Excel 工作簿", "*.xlsx")])
        if not path:
            return
        try:
            result: ExcelMatchResult = import_xlsx(path, group)
        except Exception as exc:
            self.log("ERROR", f"Excel 导入失败: {exc}")
            messagebox.showerror("Excel 导入失败", str(exc))
            return
        self.excel_mappings[group.key] = result.mapping
        self.excel_skipped[group.key] = {record.path for record in result.matched_without_name}
        self.mode_var.set("Excel 名称")
        self.apply_preview()
        self.log("INFO", f"Excel 匹配完成：成功 {result.matched_count}，未匹配文件 {len(result.unmatched_files)}，未匹配行 {len(result.unmatched_rows)}。")
        for warning in result.warnings:
            self.log("WARN", warning.removeprefix("WARN "))
        if result.unmatched_files or result.unmatched_rows:
            messagebox.showwarning("Excel 匹配预览", f"成功 {result.matched_count} 行\n未匹配文件 {len(result.unmatched_files)} 个\n未匹配 Excel 行 {len(result.unmatched_rows)} 行\n详情见日志和高亮行。")

    def _prepare_group(self, group: NamingGroup) -> None:
        if group.key == self.current_group_key:
            self.apply_preview()
        else:
            separator = self.separator_var.get()
            for record in group.records:
                record.name = record.base_name
            if self.mode_var.get() == "数字编号":
                assign_numeric(group, int(self.start_var.get()), int(self.width_var.get()), int(self.step_var.get()), self.meta_var.get(), separator)
            elif self.mode_var.get() == "Excel 名称":
                mapping = self.excel_mappings.get(group.key, {})
                skipped = self.excel_skipped.get(group.key, set())
                for record in group.records:
                    if record.path in mapping:
                        record.name = mapping[record.path]
                        record.selected = True
                    elif record.path in skipped:
                        record.selected = False
                        record.status = "Skipped"
                        record.status_detail = "Excel B 列为空"
                    record.target_name = compose_filename(self.meta_var.get(), group.prefix, record.child_prefix,
                                                          record.name, record.extension_original or record.extension, separator)
                    if record.path not in mapping and record.path not in skipped:
                        record.selected = False
            else:
                preview_group(group, self.meta_var.get(), separator)

    def rename_one(self, record: FileRecord) -> None:
        group = self.groups.get(record.group_key)
        if not group:
            return
        self._prepare_group(group)
        record.selected = True
        operation = execute_rename([record], self.history_path, kind="single", separator=self.separator_var.get())
        if any(item.success for item in operation.items):
            self.log("INFO", f"单项重命名成功: {operation.items[0].old_path} -> {operation.items[0].new_path}")
        else:
            for item in operation.items:
                self.log("ERROR", f"单项重命名失败: {item.error}")
        self.refresh_tree()

    def rename_current(self) -> None:
        group = self.current_group()
        if not group:
            messagebox.showwarning("没有命名组", "请先扫描并选择命名组。")
            return
        self._prepare_group(group)
        operation = execute_rename(group.records, self.history_path, kind="batch", separator=self.separator_var.get())
        self._log_operation(operation, group.label)
        self.refresh_tree()

    def rename_all(self) -> None:
        if not getattr(self, "groups", None):
            messagebox.showwarning("没有命名组", "请先扫描。")
            return
        total = 0
        for key, group in self.groups.items():
            if not self.group_enabled.get(key, True):
                continue
            self._prepare_group(group)
            operation = execute_rename(group.records, self.history_path, kind="batch-all", separator=self.separator_var.get())
            self._log_operation(operation, group.label)
            total += sum(item.success for item in operation.items)
        self.refresh_tree()
        self.log("INFO", f"全部已选组执行完成，成功 {total} 个文件。")

    def _log_operation(self, operation, label: str) -> None:
        success = sum(item.success for item in operation.items)
        failures = len(operation.items) - success
        if failures:
            self.log("ERROR", f"{label}: 成功 {success}，失败/冲突 {failures}。")
            for item in operation.items:
                if not item.success:
                    self.log("ERROR", item.error)
        else:
            self.log("INFO", f"{label}: 成功 {success} 个文件。")

    def undo(self) -> None:
        ok, errors = undo_last(self.history_path)
        if ok:
            self.log("INFO", "最近一次重命名已撤销。")
            if self.root_var.get():
                self.scan()
        else:
            for error in errors:
                self.log("ERROR", error)
            messagebox.showwarning("撤销未完成", "部分文件无法恢复，详情见日志。")

    def play_selected(self) -> None:
        selection = self.tree.selection()
        if selection:
            record = self.tree_records.get(selection[0])
            if record:
                self.play_record(record)
                return
        self.log("WARN", "请在文件列表中选中 WAV 文件。")

    def play_record(self, record: FileRecord) -> None:
        if record.extension.casefold() != ".wav":
            return
        try:
            self.stop_audio()
            duration = wav_duration(record.path)
            self.audio_record = record
            self.audio_duration = duration
            self.audio_paused = False
            self.audio_pause_btn.configure(text="暂停")
            self.audio_progress.configure(to=max(duration, 1))
            self.audio_progress.set(0)
            self.audio_label.configure(text=record.original_name)
            if os.name == "nt":
                self._mci(f'open "{str(Path(record.path).resolve()).replace(chr(34), chr(39))}" type waveaudio alias {self.audio_alias}')
                self._mci(f"setaudio {self.audio_alias} volume to {int(self.volume_scale.get() * 10)}")
                self._mci(f"play {self.audio_alias}")
            else:
                import winsound
                winsound.PlaySound(record.path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            self._update_audio_time(duration)
            self.log("INFO", f"播放 WAV：{record.original_name}")
        except Exception as exc:
            self.log("ERROR", f"WAV 播放失败: {exc}")

    def _mci(self, command: str) -> str:
        if os.name != "nt":
            return ""
        result = ctypes.create_unicode_buffer(256)
        error = ctypes.windll.winmm.mciSendStringW(command, result, len(result), 0)
        if error:
            message = ctypes.create_unicode_buffer(256)
            ctypes.windll.winmm.mciGetErrorStringW(error, message, len(message))
            raise OSError(message.value or f"MCI error {error}")
        return result.value

    def _update_audio_time(self, duration: float) -> None:
        if self.audio_record is None:
            return
        if os.name == "nt" and not self.audio_paused:
            try:
                position = float(self._mci(f"status {self.audio_alias} position")) / 1000
            except (ValueError, OSError):
                position = float(self.audio_progress.get())
            self.audio_progress.set(min(position, duration))
        elapsed = min(float(self.audio_progress.get()), duration)
        self.audio_time.configure(text=f"{int(elapsed)//60:02d}:{int(elapsed)%60:02d} / {int(duration)//60:02d}:{int(duration)%60:02d}")
        if duration and elapsed >= duration:
            self.stop_audio()
            return
        self.audio_job = self.after(250, lambda: self._update_audio_time(duration))

    def pause_audio(self) -> None:
        if self.audio_record is None:
            return
        if os.name == "nt":
            try:
                if self.audio_paused:
                    self._mci(f"resume {self.audio_alias}")
                    self.audio_paused = False
                    self.audio_pause_btn.configure(text="暂停")
                else:
                    self._mci(f"pause {self.audio_alias}")
                    self.audio_paused = True
                    self.audio_pause_btn.configure(text="继续")
            except OSError as exc:
                self.log("ERROR", f"WAV 暂停失败: {exc}")
        else:
            self.stop_audio()

    def _set_audio_volume(self) -> None:
        if self.audio_record is not None and os.name == "nt":
            try:
                self._mci(f"setaudio {self.audio_alias} volume to {int(self.volume_scale.get() * 10)}")
            except OSError as exc:
                self.log("WARN", f"音量调整失败: {exc}")

    def stop_audio(self) -> None:
        if os.name == "nt":
            try:
                self._mci(f"stop {self.audio_alias}")
                self._mci(f"close {self.audio_alias}")
            except OSError:
                pass
        else:
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        if self.audio_job:
            self.after_cancel(self.audio_job)
            self.audio_job = None
        self.audio_record = None
        self.audio_paused = False
        if hasattr(self, "audio_pause_btn"):
            self.audio_pause_btn.configure(text="暂停")

    def export_tables(self) -> None:
        root = self.root_var.get().strip()
        if not root:
            messagebox.showwarning("需要根目录", "请选择根目录后再导出。")
            return
        selected = [ext for ext, var in self.export_ext_vars.items() if var.get()]
        if not selected:
            messagebox.showwarning("没有扩展名", "至少选择一种扩展名。")
            return
        try:
            outputs = export_filename_tables(root, selected, self.export_hidden_var.get(), self.export_system_var.get())
        except Exception as exc:
            self.log("ERROR", f"导出失败: {exc}")
            messagebox.showerror("导出失败", str(exc))
            return
        self.export_result.configure(text=f"已生成 {len(outputs)} 个工作簿")
        self.log("INFO", f"导出完成：{len(outputs)} 个 XLSX。")
        for output in outputs:
            self.log("INFO", str(output))
        open_in_explorer(root)

    def log(self, level: str, message: str) -> None:
        if not hasattr(self, "log_text"):
            return
        self.log_text.configure(state="normal")
        timestamp = __import__("datetime").datetime.now().strftime("%H:%M:%S")
        self.log_text.insert("end", f"[{timestamp}] [{level}] {message}\n", level)
        self.log_text.see("end")
        self.log_text.configure(state="disabled")


def main() -> None:
    app = NamerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
