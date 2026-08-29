# 版权与再分发审查

审查基于当前 Git 跟踪文件、文件名和仓库内可见元数据完成。它是发布前的来源核查清单，不是法律意见；文件名只能证明存在风险线索，不能单独证明侵权。

## 结论

- `LICENSE` 中的 MIT 许可只覆盖本项目原创的源代码和文档，范围见下表。
- 未能取得来源、作者或再分发许可证明的二进制素材，不因项目采用 MIT 就自动获得许可。
- 在公开仓库、发行压缩包或可执行文件中保留“高风险”项目之前，应取得相应授权，或用自行创作/明确开放许可的替代文件替换；仅从测试流程中移除引用并不足以解决已有分发问题。

## 本次消音处理（2026-08-28）

下表风险评级描述的是处理前文件；当前文件状态以本节为准。消音只移除了可播放的声音/音符数据，不等于取得原作者授权，文件名和元数据中的品牌、作品名仍可能涉及商标或来源证明问题。

| 范围 | 已执行的处理 | 保留内容 |
| --- | --- | --- |
| test_source/source/*.wav（含大写扩展名）共 12 个 | 将每个 RIFF/WAVE 的 data 块采样字节改为零值；未改变文件大小或其他 RIFF 块。 | fmt、BWF、JUNK、LIST、smpl、ID3 等非音频块及原文件名。 |
| test_source/source/*.flac | 删除 FLAC 音频帧；保留所有 metadata block，并将 STREAMINFO 的帧大小、总采样数和 MD5 设为零。 | FLAC metadata、采样率/声道/位深等格式信息及原文件名。 |
| test_source/source/*.mid 共 3 个 | 移除 note_on、note_off、polytouch 事件，同时把被移除事件的时间累加到后续事件；文件仍可被 MIDI 解析器打开。 | 轨道、曲名、速度、控制器、程序变更、时间线和其他元事件。 |
| test_source/source/ST - BASS - Glitchy.fxp | 未修改。FXP 是插件预设容器，不包含可直接清空的 WAV/FLAC 音频流。 | 原始预设内容；它仍需单独核对预设库许可证。 |

验证结果：12 个 WAV 的 data 块非零字节数均为 0，FLAC 无剩余音频帧且总采样数为 0，3 个 MIDI 均无音符事件。Logo、test_source/tree_output.txt 和 history/history.json 未修改。

## 需要先处理的文件

| 文件 | 风险 | 依据/线索 | 发布建议 |
| --- | --- | --- | --- |
| `test_source/source/鹿乃 - ウミユリ海底譚.flac` | 高 | 文件名指向歌手“鹿乃”和具体歌曲《ウミユリ海底譚》，看起来是完整录音而非测试音。 | 未取得录音及作品许可前不要分发；建议移出仓库并改用合成短音频。 |
| `test_source/source/Undertale - Another Medium.mid` | 高 | 文件名指向游戏《Undertale》的曲目 “Another Medium”；MIDI 仍可能复制作曲/编曲表达。 | 取得权利人许可或删除，改用原创 MIDI。 |
| `test_source/source/Cymatics - Leche - 144 BPM E Min.wav` | 高 | `Cymatics` 是商业采样/音色品牌，文件名包含产品式命名和调性/BPM。 | 核对购买授权是否允许随软件/仓库再分发；无法证明则移除。 |
| `test_source/source/KSHMR Stab - Brass 03 (D).wav` | 高 | `KSHMR` 及编号式音色名称明显对应商业采样包。 | 核对厂商 EULA；通常不要把原始样本打包进软件。 |
| `test_source/source/KSHMR Synth Arp 06 (90, Dm).wav` | 高 | 同上，带商业产品名、编号、BPM/调性。 | 核对 EULA 或替换为原创测试音频。 |
| `test_source/source/BFNK - Pad_02.wav` | 高 | `BFNK`/`Pad_02` 是第三方音色库式命名，仓库没有授权文件。 | 找到来源和再分发条款；无法确认则移除。 |
| `test_source/source/CL_IE_Drums_Full_Loop_2_150.wav` | 高 | `CL_IE`、`Drums`、`Full_Loop`、编号和 BPM 组合显示采样包来源。 | 核对样本包许可证；仅为测试时可改为程序生成的短 WAV。 |
| `test_source/source/CL_IE_Drums_Perc_ChinaClick_F.wav` | 高 | 同一采样包命名模式，仓库无来源/许可证文本。 | 核对许可或移除。 |
| `test_source/source/CL_IE_Drums_Perc_DistKick.wav` | 高 | 同一采样包命名模式，仓库无来源/许可证文本。 | 核对许可或移除。 |
| `test_source/source/GPUA_Synth_Chord_Am.wav` | 高 | `GPUA`/`Synth_Chord`/调性命名疑似第三方音色素材。 | 补充原始授权证明，或替换。 |
| `test_source/source/RPS - I Remember Hyperpop Loop Gmin 184BPM (Full).wav` | 高 | 艺术家/包名加完整 Loop、BPM、调性，明显不是通用空白夹具。 | 核对商业样本 EULA；无证明不要再分发。 |
| `test_source/source/ST - BASS - Glitchy.fxp` | 高 | `.fxp` 是可加载的预设文件，名称含第三方缩写和预设名。 | 确认预设/音色库许可；最好不随项目发布。 |
| `test_source/source/SYRN_Botanica_5_C#maj.wav` | 高 | `SYRN_Botanica`、编号和调性指向第三方样本包（且历史记录出现 `[Botanica]` 路径）。 | 核对厂商许可或移除。 |
| `test_source/source/W2_Instruments_Marimba_Loops_Aura_F_150.wav` | 高 | `W2`、`Instruments`、`Marimba_Loops`、BPM/调性是商业 loop 命名特征。 | 核对来源和 EULA；无法证明则移除。 |
| `test_source/source/W2_Midi_Chords_Vibe_F#_Min_135.mid` | 高 | 同一第三方包命名模式，属于可再利用的 MIDI 表达。 | 取得 MIDI 再分发授权或改用原创 MIDI。 |
| `test_source/source/260606-183659.WAV` | 中（来源待确认） | 100 MB 的 BWF/WAV；文件头含 `ZOOM H1essential`、`SCENE=260606-183659` 和 `2026-06-06 18:36:59`，看起来是现场录音，但没有录音者或再分发许可声明。 | 若确认为项目作者本人录制，可记录为原创素材；否则取得录音及现场声音的授权，或移除。 |
| `test_source/source/kokoro.mid` | 中 | 真实 MIDI，名称和元数据可能含作者/作品信息，但当前仓库没有来源记录。 | 在 `NOTICE` 或素材清单中记录作者和许可；无法核实时移除。 |

## 来源不明或可能带来其他权利问题的文件

| 文件 | 风险 | 依据/线索 | 发布建议 |
| --- | --- | --- | --- |
| `Artwork/Logo.png` | 中 | 作为程序 Logo 使用，但没有作者、来源或许可声明。 | 确认是原创或取得图片许可；补充作者/许可后再随发行版发布。 |
| `Artwork/Logo.ico` | 中 | 与 `Artwork/Logo.png` 配套的图标，没有来源声明。 | 与 PNG 使用同一授权记录，或重新制作并留存源文件。 |
| `webui/logo.png` | 中 | SHA-256 与 `Artwork/Logo.png` 相同，是同一未说明来源的图片副本。 | 不要把复制品误写成独立原创；确认上游授权后再分发。 |
| `test_source/tree_output.txt` | 中 | 目录快照包含大量 `Cymatics`、`KSHMR`、`Botanica` 等第三方包名，并出现 `LICENSE.pdf` 条目，但许可正文不在仓库。 | 视为来源证据而非许可证明；发布前删除快照或附上可核验的授权文件。 |
| `history/history.json` | 中（隐私/来源） | 含 `E:\packs\...` 等本地路径、样本包名称和操作记录；不是作品许可文件。 | 发布前清理个人路径和历史数据，或加入忽略规则；不要把它当作素材授权依据。 |

## 低风险/当前可按 MIT 处理的内容

| 文件范围 | 判断 |
| --- | --- |
| `main.py`、`namer_app.py`、`namer_core.py`、`web_app.py`、`webui/app.js`、`webui/index.html`、`webui/styles.css`、`tests/*.py`、`install_environment.bat`、`start_webui.bat`、`README.md`、`USER_GUIDE.md`、`version_info.txt`、`requirements.txt` | 当前仓库没有发现外部代码复制声明；在作者确认这些文件为原创后，可按 `LICENSE` 中的 MIT 条款发布。 |
| `demo_empty_tree/**/*` | 所有示例音频/MIDI 文件当前为 0 字节占位文件，没有可播放的作品内容；可继续作为测试路径夹具使用。 |

## 依赖

`requirements.txt` 只声明 `openpyxl`。它是独立的第三方项目，分发可执行文件时应遵守其上游许可证并保留相应版权/许可文本；本仓库的 MIT 文件不改变 `openpyxl` 的许可证。

## 建议的发布门槛

1. 先移除或替换所有“高风险”二进制文件，并让测试使用仓库内的 0 字节夹具或运行时生成的短样本。
2. 为 Logo 和 `kokoro.mid` 找到可核验的作者与许可，写入单独的 `NOTICE`/素材清单。
3. 清理 `history/history.json` 和外部目录快照，避免泄露本地路径或让目录快照被误解为授权证明。
4. 发布前重新运行 `git ls-files` 和发行包内容检查，确保未把构建目录中的副本重新打包进去。
