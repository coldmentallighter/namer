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
- 音频预览使用浏览器原生音频控件，支持 `.wav`、`.wv`、`.mp3`、`.aif`、`.flac`、`.aac`、`.ogg`、`.m4a`（实际解码能力取决于 Windows 浏览器内置编解码器）。
- 左侧命名组区域固定在视口内独立滚动，不会因扩展名或文件夹过多撑长主页面；右上角可切换并记住深色模式。
- 导出根目录存在三级或更多层嵌套时，会额外生成根目录下的 `filetree.txt` 索引；它只列出本次生成 `.ffnf.xlsx` 的内容目录和对应表格路径，不会展开所有文件。为兼容旧版本，同时保留记录完整目录树的 `Structure.ffnf.txt`。
- 历史文件保存到源代码/程序目录下的 `history\\history.json`，可在重启后撤销最近一次操作；撤销和还原通过文件 ID、SHA-256 与大小确认身份，预期文件名变化时会在原目录寻找唯一指纹候选；首次成功重命名时自动创建 `history` 文件夹。
- 命名页支持目录层级映射、文件名模板解析预览和跨格式同 stem 关联；解析默认只展示结果，勾选使用 `name` 后才会参与目标文件名。
- 批量重命名及撤销/还原均先统一预检查，再通过临时文件名提交；支持事务内文件名互换和仅大小写变更，运行时失败会尝试整体补偿回滚。名称模式使用可逆的基础名称，导出页刷新不会清空正在编辑的命名任务。
- 导出页生成详细 XLSX：文件名为 `<目录>.ffnf.xlsx`（冲突时使用 `<目录>.oriNN.ffnf.xlsx`），扩展名工作表包含 `SourceName`/`NewName`、`BPM` 和空白 `Scale` 列与文件元数据，并附带 `Metadata`、`Summary` 统计工作表；Excel B 列支持 `{bpm}`/`{scale}` 模板。

## 构建可执行文件

可选使用 PyInstaller（离线环境需预先准备其安装包）：

```powershell
python -m pip install pyinstaller openpyxl
python -m PyInstaller --clean --noconfirm OfflineFileNamer.spec
```

生成的 `dist\\OfflineFileNamer.exe` 使用 `Artwork\\Logo.ico` 作为程序图标，并写入版本 `1.0`、作者 `ColdLightVibe` 元数据；程序可在同类 Windows 环境离线运行，打包后的历史记录位于 exe 同级的 `history\\history.json`。

## 测试

```powershell
python -m unittest discover -s tests -v
```

测试覆盖扫描和动态扩展名、目录层级映射、自然排序、中文/特殊字符、数字命名、文件名解析、跨格式同 stem 关联、冲突阻止、事务回滚、单项与批量撤销/还原、指纹保护、两种 XLSX 匹配模式、详细 XLSX 导出与目录统计、隐藏文件、生成表格忽略、`.ori` 编号，以及本地 WebUI 的扫描/预览/映射/导出 API。

已知限制：浏览器安全模型无法直接把资源管理器拖入的文件夹绝对路径交给本地服务，因此 WebUI 默认使用 Windows 原生文件夹选择器和路径输入；后续可接入原生 WebView 容器实现资源管理器拖拽路径桥接。

## 许可证与素材版权

本项目原创源代码和文档采用 [MIT License](LICENSE)。MIT 许可不自动覆盖第三方依赖、样本音频/MIDI、插件预设、Logo 或历史记录等非代码素材；这些文件的来源、风险和发布前处理建议见 [版权与再分发审查](COPYRIGHT_REVIEW.md)。发布源码或可执行文件前，请先取得相应素材的再分发授权，或移除/替换来源不明的素材。
