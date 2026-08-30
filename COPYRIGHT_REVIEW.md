# 版权与再分发审查

审查基于当前 Git 跟踪文件、文件名和仓库内可见元数据完成。它是发布前的来源核查清单，不是法律意见；文件名只能证明存在风险线索，不能单独证明侵权。

## 结论

- `LICENSE` 中的 MIT 许可只覆盖本项目原创的源代码和文档，范围见下表。
- 未能取得来源、作者或再分发许可证明的二进制素材，不因项目采用 MIT 就自动获得许可。
- 在公开仓库、发行压缩包或可执行文件中保留“高风险”项目之前，应取得相应授权，或用自行创作/明确开放许可的替代文件替换；仅从测试流程中移除引用并不足以解决已有分发问题。

## 测试资源占位化（2026-08-30）

`test-source` 下的 672 个测试资源现均为 0 字节占位文件，仅保留目录、文件名和扩展名，用于扫描、筛选和文件名解析回归；其中不再保留音频、音符、图像、预设、文档正文或原始 metadata。需要验证 WAV/MIDI metadata 的单元测试会在临时目录中生成无音频数据、无音符事件的最小合成文件，测试结束后自动删除。

下表风险评级描述的是占位化前的来源线索。保留第三方品牌或作品名只用于命名解析测试，不代表获得商标、作品或素材的再分发授权。

## 需要先处理的文件

| 文件 | 风险 | 依据/线索 | 发布建议 |
| --- | --- | --- | --- |
| `test-source/source-audio/鹿乃 - ウミユリ海底譚.flac` | 高 | 文件名指向歌手“鹿乃”和具体歌曲《ウミユリ海底譚》，看起来是完整录音而非测试音。 | 未取得录音及作品许可前不要恢复原内容；测试应继续使用占位符。 |
| `test-source/source-audio/Undertale - Another Medium.mid` | 高 | 文件名指向游戏《Undertale》的曲目 “Another Medium”；MIDI 仍可能复制作曲/编曲表达。 | 不要恢复原 MIDI；metadata 测试使用运行时合成文件。 |
| `test-source/source-audio/Cymatics - Leche - 144 BPM E Min.wav` | 高 | `Cymatics` 是商业采样/音色品牌，文件名包含产品式命名和调性/BPM。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/KSHMR Stab - Brass 03 (D).wav` | 高 | `KSHMR` 及编号式音色名称明显对应商业采样包。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/KSHMR Synth Arp 06 (90, Dm).wav` | 高 | 同上，带商业产品名、编号、BPM/调性。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/BFNK - Pad_02.wav` | 高 | `BFNK`/`Pad_02` 是第三方音色库式命名，仓库没有授权文件。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/CL_IE_Drums_Full_Loop_2_150.wav` | 高 | `CL_IE`、`Drums`、`Full_Loop`、编号和 BPM 组合显示采样包来源。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/CL_IE_Drums_Perc_ChinaClick_F.wav` | 高 | 同一采样包命名模式，仓库无来源/许可证文本。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/CL_IE_Drums_Perc_DistKick.wav` | 高 | 同一采样包命名模式，仓库无来源/许可证文本。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/GPUA_Synth_Chord_Am.wav` | 高 | `GPUA`/`Synth_Chord`/调性命名疑似第三方音色素材。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/RPS - I Remember Hyperpop Loop Gmin 184BPM (Full).wav` | 高 | 艺术家/包名加完整 Loop、BPM、调性，明显不是通用空白夹具。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/ST - BASS - Glitchy.fxp` | 高 | `.fxp` 是可加载的预设文件，名称含第三方缩写和预设名。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/SYRN_Botanica_5_C#maj.wav` | 高 | `SYRN_Botanica`、编号和调性指向第三方样本包。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/W2_Instruments_Marimba_Loops_Aura_F_150.wav` | 高 | `W2`、`Instruments`、`Marimba_Loops`、BPM/调性是商业 loop 命名特征。 | 无明确授权时只保留 0 字节命名夹具。 |
| `test-source/source-audio/W2_Midi_Chords_Vibe_F#_Min_135.mid` | 高 | 同一第三方包命名模式，属于可再利用的 MIDI 表达。 | 不要恢复原 MIDI；metadata 测试使用运行时合成文件。 |
| `test-source/source-audio/260606-183659.WAV` | 中（来源待确认） | 占位化前是 100 MB BWF/WAV，文件头曾含现场录音设备和时间信息。 | 即使确认原创，仓库测试也无需恢复该录音。 |
| `test-source/source-audio/kokoro.mid` | 中 | 占位化前是真实 MIDI，来源记录不完整。 | 不要恢复原 MIDI；metadata 测试使用运行时合成文件。 |

## 来源不明或可能带来其他权利问题的文件

| 文件 | 风险 | 依据/线索 | 发布建议 |
| --- | --- | --- | --- |
| `Artwork/Logo.png` | 中 | 作为程序 Logo 使用，但没有作者、来源或许可声明。 | 确认是原创或取得图片许可；补充作者/许可后再随发行版发布。 |
| `Artwork/Logo.ico` | 中 | 与 `Artwork/Logo.png` 配套的图标，没有来源声明。 | 与 PNG 使用同一授权记录，或重新制作并留存源文件。 |
| `webui/logo.png` | 中 | SHA-256 与 `Artwork/Logo.png` 相同，是同一未说明来源的图片副本。 | 不要把复制品误写成独立原创；确认上游授权后再分发。 |
| `test-source/tree_output.txt` | 低（已占位化） | 占位化前的目录快照包含大量第三方包名和本地目录信息。 | 保持为 0 字节占位符，不再提交真实目录快照。 |
| `history/history.json` | 中（隐私/来源） | 含 `E:\packs\...` 等本地路径、样本包名称和操作记录；不是作品许可文件。 | 发布前清理个人路径和历史数据，或加入忽略规则；不要把它当作素材授权依据。 |

## 低风险/当前可按 MIT 处理的内容

| 文件范围 | 判断 |
| --- | --- |
| `main.py`、`namer_core.py`、`web_app.py`、`webui/app.js`、`webui/index.html`、`webui/styles.css`、`tests/*.py`、`install_environment.bat`、`start_webui.bat`、`README.md`、`USER_GUIDE.md`、`version_info.txt`、`requirements.txt` | 当前仓库没有发现外部代码复制声明；在作者确认这些文件为原创后，可按 `LICENSE` 中的 MIT 条款发布。 |
| `demo_empty_tree/**/*`、`test-source/**/*` | 所有示例和测试资源当前为 0 字节占位文件，没有可播放或可还原的作品内容；可继续作为路径、扩展名和文件名夹具使用。 |

## 依赖

`requirements.txt` 只声明 `openpyxl`。它是独立的第三方项目，分发可执行文件时应遵守其上游许可证并保留相应版权/许可文本；本仓库的 MIT 文件不改变 `openpyxl` 的许可证。

## 建议的发布门槛

1. 保持所有测试资源为 0 字节夹具；格式 metadata 测试只使用运行时生成且不含作品内容的最小文件。
2. 为 Logo 和 `kokoro.mid` 找到可核验的作者与许可，写入单独的 `NOTICE`/素材清单。
3. 清理 `history/history.json` 和外部目录快照，避免泄露本地路径或让目录快照被误解为授权证明。
4. 发布前重新运行 `git ls-files` 和发行包内容检查，确保未把构建目录中的副本重新打包进去。
