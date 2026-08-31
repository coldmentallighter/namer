# 上帝模块拆解与模块整合计划

> 依据对全部源码的逐行审读与依赖分析制定。基线：`python -m unittest discover -s tests` 全量通过（80 用例 OK，3 跳过）。
> 本计划只做**纯搬移与重组，不改变任何行为**；每阶段独立可回退。

---

## 1. 目标

1. 拆解两个上帝模块：`core/files.py`（1,454 行）、`workflow_system/catalog.py`（1,204 行）。
2. 判定其他模块是否存在"联系过多、应当整合"的情况，给出整合/保持清单。
3. 顺带切断已知耦合点：`engine` 对 `workflow_system.catalog` 的依赖（R3）。

**铁律**：`web_app.py` 组合根不动；所有文件/类/函数签名不变；测试全程保持全绿；无 git 仓库时先建仓再动手。

---

## 2. 现状基线

| 模块 | 行数 | 依赖 | 角色 |
|---|---|---|---|
| core/files.py | 1,454 | 仅 stdlib + openpyxl | 领域核心（18 个职责域混杂） |
| workflow_system/catalog.py | 1,204 | runtime | 校验 + 打包 + 热加载目录（6 个职责域混杂） |
| workflow_system/runtime.py | 343 | 仅 stdlib | 动态模块加载（安全边界） |
| workflow_system/metadata.py | 92 | core.files, runtime, catalog(惰性) | 能力分发门面 |
| workflow_system/values.py | 341 | 仅 stdlib + openpyxl | 标签值持久化（独立域） |
| engine/rules.py | 205 | core.files | 表达式/条件 DSL 求值 |
| engine/composer.py | 158 | core.files, catalog, rules | 目标名组合与冲突消解 |
| engine/candidates.py | 76 | core.files | 模块请求构造/结果适配 |
| engine/executor.py | 56 | core.files, composer, candidates, rules | 引擎门面（DI 注入点） |
| engine/session.py | 445 | core.files, catalog, executor, rules | 有状态会话编排 |
| server/*（10 文件） | 1,191 | core, catalog, engine | HTTP 层 |
| web_app.py | 488 | 全部 | 组合根（闭包注入） |
| tests/ | 2,032 | 端到端为主 | 80 用例 |

---

## 3. 核心结论速览

| # | 动作 | 对象 | 判断 |
|---|---|---|---|
| 1 | 拆解 | core/files.py → 8 个模块 | ✅ 必做，收益最大 |
| 2 | 拆解 | workflow_system/catalog.py → 3 个模块 | ✅ 必做 |
| 3 | 整合 | engine/executor.py + engine/candidates.py | ✅ 建议（132 行，单一职责域） |
| 4 | 整合 | workflow_system/runtime.py + workflow_system/metadata.py | ✅ 建议（435 行，消灭惰性导入） |
| 5 | 移入 core | server/history.py → core/history.py | ✅ 建议（与 undo/redo 同域） |
| 6 | 切断耦合 | engine 改依赖 schema 纯模块，不再依赖 catalog | ✅ 必做（R3 解除） |
| 7 | 保持 | engine/rules.py、engine/composer.py 分离 | ⚠️ 不合并（DSL 求值 vs 命名组合） |
| 8 | 保持 | server/scanning.py + server/associations.py | ⚠️ 可选合并，默认不合并 |
| 9 | 保持 | server/controllers/*（8 个）、engine/session.py、values.py、web_app.py | ❌ 不整合 |

---

## 4. `core/files.py` 拆分设计

### 4.1 目标结构

```
core/
  __init__.py
  files.py        ← 兼容垫片（第一阶段保留：re-export 全部名字），最终删除
  models.py       ← 8 个 dataclass：FileRecord、NamingGroup、ScanResult、LogEntry、
                     RenameItem、RenameOperation、ValidationIssue、ExcelMatchResult
  fsutil.py       ← natural_key、normalise_ext、ILLEGAL_CHARS、RESERVED_NAMES、
                     is_hidden、is_system、is_generated_workbook、is_generated_structure、
                     _windows_file_attributes、read_file_metadata、open_in_explorer、_path_case_key
  scan.py         ← directory_prefix_defaults、scan_folder、build_stem_associations、refresh_stem_associations
  naming.py       ← compose_filename、_TEMPLATE_FIELDS、_template_regex、parse_filename、
                     apply_filename_parse、preview_group、assign_numeric
  validate.py     ← validate_filename、preflight
  rename.py       ← file_fingerprint、_fingerprint_matches、_resolve_history_source、
                     _move_history_items、execute_rename
  history.py      ← append_history、_write_history、_history_action_indices、undo_last、redo_last
                     （整合后吸收 server/history.py 的 read_snapshot、changed_items、change_description）
  xlsx.py         ← _excel_value、_match_key、_excel_name、_EXCEL_NAME_TEMPLATE、
                     _expand_excel_name_template、import_xlsx、_unique_export_path、
                     _structure_directories、_write_filetree_export、collect_directory_statistics、
                     _flatten_metadata、export_filename_tables
```

### 4.2 新模块依赖图（保持无环）

```
models.py（叶） ← fsutil.py（叶）
scan.py      → models, fsutil
naming.py    → models, fsutil
validate.py  → models, fsutil
rename.py    → models, validate, fsutil
history.py   → models, rename, fsutil
xlsx.py      → models, scan, fsutil
```

无循环：`history → rename → validate → fsutil/models`；`xlsx → scan`。openpyxl 只留在 xlsx.py，可后续升级为惰性导入。

### 4.3 拆解边界说明（为什么这样切）

- **models 单独成模块**：8 个 dataclass 被所有层引用，是依赖最密集的节点，独立后每个模块的导入面大幅收窄。
- **fsutil 聚合"Windows/路径小工具"**：`is_hidden`/`is_system`/`open_in_explorer` 是 Windows 专有代码，聚合后 Windows 特化代码集中一处（缓解 R8）。
- **rename.py 自含事务与指纹**：`execute_rename`（两阶段改名 + 回滚）与指纹识别是原子性核心（R5），独立模块便于后续单独审计。
- **xlsx.py 独占 openpyxl**：导入/导出/统计是唯一触碰 openpyxl 的地方，独立后可整体替换或惰性化。

### 4.4 兼容策略

1. 先建新模块，把函数**原样搬移**（保持 docstring 与签名）。
2. `core/files.py` 改为 re-export 垫片（`from .models import *` + 显式名字表），此时**全部现有 import 不破**，跑测试确认。
3. 逐个更新调用方 import（engine → workflow_system → server → web_app → tests），每批后跑测试。
4. 全部迁移完成后删除垫片。

---

## 5. `workflow_system/catalog.py` 拆分设计

### 5.1 目标结构

```
workflow_system/
  __init__.py
  catalog.py     ← 保留：RESOURCE_WORKFLOW_ROOT、workflow_root_signature、discover_workflows、
                     workflow_summary、WorkflowCatalog；对 schema/package 做 re-export 垫片
  schema.py      ← 新建（纯校验，零 I/O）：WORKFLOW_SCHEMA_VERSION、WORKFLOW_FILE_NAME、
                     _FIELD_ID、_METADATA_PATH、_RULE_OPERATORS、_EXPRESSION_OPERATORS、
                     _ACTION_ID、_ACTION_KINDS、_CONTEXT_ROOTS、_INITIAL_SOURCES、_MODULE_TRIGGERS、
                     _normalise_template、_validate_condition、_validate_expression、_normalise_derived、
                     _normalise_rules、_normalise_metadata_providers、_normalise_workflow_modules、
                     _normalise_actions、_normalise_numbering、_normalise_profiles、
                     validate_workflow、workflow_field_map、CORE_FALLBACK_WORKFLOW
  package.py     ← 新建（打包/导入）：WORKFLOW_PACKAGE_EXTENSION、package_workflow、
                     load_workflow_bundle、load_workflow_package、copy_workflow、_public_workflow
```

依赖：`schema.py`（叶）← `package.py` ← `catalog.py`；`catalog.py → runtime`。无环。

### 5.2 为什么这样切

- **schema.py 是纯函数域**（约 590 行校验 + 常量）：无文件系统、无模块加载。engine 的 `composer.py`、`session.py` 只需 `workflow_field_map`，改从 `schema` 导入后，**engine 与 catalog（含热加载、打包、动态模块）彻底解耦** —— 这是本次拆解顺带完成 R3 的关键动作。
- **package.py 是打包/导入域**：zip 安全校验（512 项/64MB 限制、路径穿越检查）集中一处。
- **catalog.py 只剩运行时类**：`WorkflowCatalog`（热加载、config.json、installed-workflows 安装事务）+ 发现/签名/摘要。

### 5.3 兼容策略

同 4.4：catalog.py 先做 re-export 垫片，调用方（application.py、controllers、tests）逐批迁移；重点把 `engine/composer.py`、`engine/session.py` 的 `from workflow_system.catalog import workflow_field_map` 改为 `from workflow_system.schema import workflow_field_map`。

---

## 6. 其他模块整合判断

### 6.1 建议整合（3 处）

**① engine/executor.py + engine/candidates.py（56 + 76 = 132 行）**
- 依据：`candidates`（`module_items`/`apply_module_result`）全项目只有 `executor.py` 一个调用方；两者同属"工作流模块执行协议"域（请求构造 → 结果校验 → 候选落地）。
- 做法：把 candidates 的函数并入 executor.py 的 `WorkflowEngine` 方法体附近（或同文件平级函数）；删除 candidates.py。测试无直接 import（test_workflow_api 走 HTTP 全链路），风险极低。

**② workflow_system/runtime.py + workflow_system/metadata.py（343 + 92 = 435 行）**
- 依据：`metadata.py` 是 `runtime` 注册表的薄分发门面（provider/normalizer/parser 三个 getter 的调用封装）；两文件职责同一。合并后 `_default_registry()` 的**惰性导入（metadata → catalog）循环规避代码消失**，模块边界更干净。
- 注意：合并后 runtime.py 将依赖 `core.files`（`read_file_metadata`/`FileRecord`）——方向合法（runtime 本就在 core 之上）。
- 兼容：保留 `metadata.py` 作为垫片（web_app、test_namer 从它 import 4 个函数），迁移后删除。

**③ server/history.py → core/history.py（56 行搬家）**
- 依据：`read_snapshot`/`changed_items`/`change_description` 是 undo/redo 历史的"读与 diff"层，而 `append_history`/`undo_last`/`redo_last` 已在 core/files.py 里 —— 同一领域被切成两半。core 拆分后合并进 `core/history.py`，`server/history.py` 变垫片再删。
- 调用方仅 web_app.py 与 operations.py，改动面小。

### 6.2 不整合（各有独立职责，合并反损清晰度）

| 组合 | 行数 | 不整合理由 |
|---|---|---|
| engine/rules.py + composer.py | 205+158 | rules 是**表达式 DSL 求值器**（被 test_engine_rules 直接单测），composer 是**命名组合与冲突消解**；合并成 363 行混合模块得不偿失 |
| server/scanning.py + associations.py | 158+135 | 都协调扫描态但生命周期不同：scan 在扫描/分组时，association 在选中/展开/撤销时；合并需引入调用时序约定 |
| server/controllers/* 8 个 | 948 | 与 POST 路由 1:1 对应，合并即成上帝控制器 |
| engine/session.py | 445 | 有状态编排层，与无状态 engine 分离是正确边界 |
| workflow_system/values.py | 341 | 独立持久化域（openpyxl 工作簿），无共享代码 |
| webui/app.js + tag-manager.js | — | 前端已按功能分离，无整合需要 |

### 6.3 耦合现状判定（回答"相互联系是否过多"）

联系确实密集，但**都是结构性的分层依赖，不是合并信号**：
- `server/state.py` 是 hub（14 个消费者）—— 这是 R1，靠"不拆进程"缓解，不靠合并模块。
- `server/controllers` 全部通过 web_app 闭包注入依赖，模块间几乎无直接 import —— 已经是松散结构。
- 唯一"联系过多且应当切断"的是 **engine → catalog**（R3），由 6.2 之外的第 5 节方案解决。
- 其余密集联系（catalog ↔ runtime、core ↔ 各层）方向合法、层次正确。

---

## 7. 分阶段执行计划（每阶段以"测试全绿"为关口）

### 阶段 0：前置（必须先做）
1. `git init` + 初始提交（当前目录**不是 git 仓库**，拆解无版本控制风险不可控）。
2. 复跑基线测试，记录 80 OK / 3 skipped。
3. 删除探测残留 `.tmp-tests/`。

### 阶段 1：拆 core/files.py（核心收益）
1. 建 `core/models.py`、`core/fsutil.py`（纯搬移）。
2. 建 `core/scan.py`、`core/naming.py`、`core/validate.py`（纯搬移）。
3. 建 `core/rename.py`、`core/history.py`、`core/xlsx.py`（纯搬移）。
4. `core/files.py` 改垫片 → 测试全绿。
5. 更新调用方：engine → workflow_system/metadata → server/* → web_app.py → tests，每批跑测试。
6. 删除垫片 → 测试全绿。

### 阶段 2：拆 workflow_system/catalog.py
1. 建 `workflow_system/schema.py`、`workflow_system/package.py`（纯搬移）。
2. catalog.py 改垫片 → 测试全绿。
3. 迁移调用方；**同时把 engine/composer.py、engine/session.py 改为从 schema 导入 `workflow_field_map`**（R3 解除）→ 测试全绿。
4. 删除垫片 → 测试全绿。

### 阶段 3：整合（3 处，每处独立提交）
1. candidates.py 并入 executor.py。
2. metadata.py 并入 runtime.py（垫片过渡）。
3. server/history.py 并入 core/history.py（垫片过渡）。

### 阶段 4：验证与收尾
1. 全量测试 + 手动冒烟：启动 `python main.py` → 扫描 `test-source/` → 预览/重命名 → 撤销 → 导出 XLSX → 切换 workflow → 确认热加载。
2. 更新 README（模块图、测试覆盖描述）。
3. 检查最终 import 面：`grep -r "from core.files" ` 应只剩文档/垫片引用；`from workflow_system.catalog import workflow_field_map` 应清零。

---

## 8. 风险与缓解

| 风险 | 等级 | 缓解 |
|---|---|---|
| 搬移引入笔误（缩进/漏函数） | 中 | 每阶段测试关口；搬移采用"整段剪切"而非重打；diff 审查 |
| import 面遗漏（运行期才炸） | 中 | 垫片阶段保留全部名字；`python -c "import web_app"` 冒烟；80 测试多为端到端，覆盖 import |
| 垫片删除过早 | 低 | 按"先迁调用方、后删垫片"顺序执行 |
| openpyxl 惰性化副作用 | 低 | 本计划不惰性化，xlsx.py 保持模块级 import，行为零变化 |
| engine→schema 迁移破坏校验顺序 | 低 | workflow_field_map 是纯查表，无副作用 |
| 无 git 回退能力 | 高 | 阶段 0 强制 git init + 初始提交；每阶段独立提交 |

---

## 9. 验收标准

1. `python -m unittest discover -s tests`：80 OK / 3 skipped，与基线一致。
2. 手动冒烟全通过（见阶段 4）。
3. `core/files.py`、`workflow_system/catalog.py` 垫片删除后，无任何 `from core.files` / `from workflow_system.catalog import <校验函数>` 残留（除文档）。
4. `engine/` 不再 import `workflow_system.catalog`（R3 解除）。
5. 除 import 语句与文件位置外，git diff 中无函数体改动。

---

## 10. 明确不做（防范围蔓延）

- 不改 `web_app.py` 组合根结构（拆解后它仍是唯一组装点）。
- 不拆 `StateManager`（R1 是进程级拆解的前提，本次不动）。
- 不做进程/服务化（见上一轮评估：价值低、风险高）。
- 不合并 engine/rules 与 composer、controllers、session、values。
