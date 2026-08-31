# 离线批量文件命名器

完整操作说明请参阅 [USER_GUIDE.md](USER_GUIDE.md)。

Windows 本地离线 WebUI 应用。Python 内置 HTTP 服务只绑定 `127.0.0.1`，浏览器界面由项目内静态文件提供，不调用网络服务；Excel 读写使用 `openpyxl`。

## 启动

在安装了 Python 3.10+（包括 Python 3.13）的 Windows 终端中执行：

```powershell
python -m pip install -r requirements.txt
python main.py
```

也可以直接双击项目根目录的 `start_webui.bat` 一键启动。

首次使用可以先双击 `install_environment.bat`。它会在项目目录创建 `.venv` 并安装 `requirements.txt` 中的依赖；之后启动脚本会优先使用该环境。

启动后会自动打开本机浏览器中的 WebUI，并在终端打印本地地址。关闭 WebUI 标签页后，本地服务会在几秒内自动退出；刷新页面会保留服务。首次打开后输入路径或点击“选择”打开 Windows 文件夹选择器，再点击“扫描”。

## 使用要点

- 扫描会递归遍历所有子目录，动态统计扩展名；`.ffnf.xlsx` 和 `.oriNN.ffnf.xlsx` 永远视为软件生成物并跳过。
- 命名组按“相对目录 + 扩展名”隔离，每组有独立组前缀和数字序列；左侧显示完整相对路径，并可拖动分隔条调整宽度。方案一默认将文件所在路径末端三个目录映射为元前缀、组前缀、子前缀，浅层目录按根目录回退。
- 执行前会检查非法字符、目标存在、目标重复、源文件缺失、权限/占用和路径过长；任何冲突会阻止该组，不会覆盖文件。
- 音频预览根据扫描得到的通用 MIME metadata 判断是否显示，实际解码能力取决于 Windows 浏览器内置编解码器；核心不维护音频扩展名清单。
- 左侧命名组区域固定在视口内独立滚动，不会因扩展名或文件夹过多撑长主页面；右上角可切换并记住深色模式。
- 导出根目录存在三级或更多层嵌套时，会额外生成根目录下的 `filetree.txt` 索引；它只列出本次生成 `.ffnf.xlsx` 的内容目录和对应表格路径，不会展开所有文件。
- 历史文件保存到源代码/程序目录下的 `history\\history.json`，可在重启后撤销最近一次操作；撤销和还原通过文件 ID、SHA-256 与大小确认身份，预期文件名变化时会在原目录寻找唯一指纹候选；首次成功重命名时自动创建 `history` 文件夹。
- 命名页支持目录层级映射、文件名模板解析预览和跨格式同 stem 关联；解析默认只展示结果，勾选使用 `name` 后才会参与目标文件名。
- 命名页动态发现 `workflows/<插件目录>/workflow.json`，不维护固定工作流名单；安装、修改或移除任意数量的工作流都会在运行中刷新。目录还可带 `module-manifest.json` 和 `modules/`，其中的 provider、normalizer、文件名解析器和 runner 与该工作流一起加载、刷新和卸载，不会注册成跨工作流全局能力。
- 顶栏“工作流管理”是独立管理页：表格列出全部工作流（软件内置 / 外部安装 / 配置型）与启用、信任、诊断状态，支持两步导入（先预检包内容与哈希、确认冲突策略与模块信任后再安装）、导出、停用/启用、卸载与清除数据。停用的工作流不加载任何模块代码；卸载先把安装目录移入 `installed-workflows/.trash` 隔离区，刷新验证通过后才清理，失败自动恢复，标签词库与用户字段值默认保留。安装记录与代码指纹保存在 `config.json` 的 `installations`，代码变更会标记“代码已变化”以便重新确认信任。
- 扫描会为每个文件生成通用 `metadata.file`；当前 workflow 通过 `metadata_providers` 声明是否读取图像尺寸、采样包 BPM 等专用 metadata。模块 runner 可在 `on_user_request` 或 `after_scan` 时接收主程序分配的临时 item ID，并只返回“item ID + 输出槽 + 字符串”；输出槽由 workflow 绑定到稳定字段，结果先进入候选值，确认后才参与命名。
- 软件配置保存在程序目录的 `config.json`；动态安装的模块工作流位于 `installed-workflows`。单个目录插件或模块缺失、无效不会阻止 WebUI 启动，零插件时自动进入基础模式。`.ffnf-workflow` 可同时携带模块清单与源码；因为受信任模块会在主进程执行，导入时必须显式确认来源可信，未知来源模块应等待后续隔离运行器。
- 提供 [workflowgenerator.md](workflowgenerator.md) 作为 AI 工作流生成规范；其他 AI 按其中的连续访谈流程收集需求，生成并校验可导入的 `workflow.json` 或 `.ffnf-workflow`。
- 批量重命名及撤销/还原均先统一预检查，再通过临时文件名提交；支持事务内文件名互换和仅大小写变更，运行时失败会尝试整体补偿回滚。名称模式使用可逆的基础名称，导出页刷新不会清空正在编辑的命名任务。
- 导出页生成详细 XLSX：文件名为 `<目录>.ffnf.xlsx`（冲突时使用 `<目录>.oriNN.ffnf.xlsx`），扩展名工作表包含 `SourceName`/`NewName`、通用文件信息和当前 workflow 读取到的动态 `Metadata.*` 列，并附带 `Metadata`、`Summary` 统计工作表；Excel 占位符由当前 workflow 的 `excel_placeholders` 声明，`{bpm}` / `{key_or_chord}` 因此只属于采样包 workflow。

## 模块结构

领域核心按职责拆为无环小模块，`web_app.py` 是唯一组合根（组装点）：

```
core/
  models.py    8 个数据类（FileRecord/NamingGroup/ScanResult/…）
  fsutil.py    Windows 属性、路径/扩展名等小工具
  scan.py      目录扫描、命名分组、跨格式 stem 关联
  naming.py    文件名组合、模板解析、数字命名
  validate.py  文件名校验与重命名预检
  rename.py    两阶段事务改名与文件指纹（原子性核心）
  history.py   撤销/还原持久化与历史读/diff
  xlsx.py      openpyxl 导入/导出/目录统计（唯一触碰 openpyxl 处）
engine/
  rules.py     表达式/条件 DSL 求值（无状态）
  composer.py  目标名组合与冲突消解
  executor.py  引擎门面 + 工作流模块执行协议（请求构造与候选落地）
  session.py   有状态会话编排
workflow_system/
  schema.py    工作流 schema 纯校验（零 I/O，engine 只依赖它）
  package.py   工作流打包/导入（zip 大小与路径安全检查）
  catalog.py   热加载目录、发现/签名/摘要与安装事务（运行时类）
  runtime.py   动态模块加载安全边界 + 能力分发门面
  values.py    标签值持久化
server/         HTTP 层：state、routes、controllers（与 POST 路由 1:1）
```


## 构建可执行文件

可选使用 PyInstaller（离线环境需预先准备其安装包）：

```powershell
python -m pip install pyinstaller openpyxl
python -m PyInstaller --clean --noconfirm OfflineFileNamer.spec
```
notice：构建尚存问题我暂时懒得改


## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖扫描和动态扩展名、workflow provider 隔离、模块热加载与坏插件隔离、字符串输出协议、metadata 派生与规则候选、目录层级映射、自然排序、中文/特殊字符、数字命名、文件名解析、跨格式同 stem 关联、冲突阻止、事务回滚、单项与批量撤销/还原、指纹保护、两种 XLSX 匹配模式、详细 XLSX 导出与目录统计，以及本地 WebUI/API。基线 80 用例全绿（3 个跳过，需要环境相关资源）。

已知限制：浏览器安全模型无法直接把资源管理器拖入的文件夹绝对路径交给本地服务，因此 WebUI 默认使用 Windows 原生文件夹选择器和路径输入；后续可接入原生 WebView 容器实现资源管理器拖拽路径桥接。

## 许可证与素材版权

本项目原创源代码和文档采用 [MIT License](LICENSE)。MIT 许可不自动覆盖第三方依赖、样本音频/MIDI、插件预设、Logo 或历史记录等非代码素材；
