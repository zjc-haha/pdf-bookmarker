# PDF 书签工具：给后续会话的工作说明

本文件适用于整个仓库。开始新需求前，先读本文件和 `README.md`，再核对当前代码、`git status`、远端提交及 Release；下面的版本状态只是 **2026-09-27 的快照**。

## 当前进度

- 公开仓库：<https://github.com/zjc-haha/pdf-bookmarker>。截至本次记录，最近的正式 Release 为 **v0.6.9**；后续会话应重新检查远端分支、标签和 Release。
- 当前源码版本为 **0.6.10rc1 测试构建**，按用户要求发布为预发布版 <https://github.com/zjc-haha/pdf-bookmarker/releases/tag/v0.6.10rc1>，发布说明在 `packaging/release-notes/v0.6.10rc1.md`：书签层级改为编号优先，并修复罗马数字编号标题下单编号条目的层级。最新正式版 0.6.9 由 GitHub Actions 构建，发布说明在 `packaging/release-notes/v0.6.9.md`，已并入 0.6.9rc7 至 0.6.9rc11 的改动。发布状态以 GitHub Release 的标签和 Assets 为准。
- v0.6.10rc1 的界面、数据目录、跨盘覆盖、书签层级和 pdfminer 失败后备处理见 `README.md` 的“当前界面”一节。后续会话以当时的代码、`git status` 和远端状态为准。

## 已确定的产品要求

- **识别只用 DeepSeek**。不要恢复本地 OCR 引擎、OCR 模式选择、外部 OCR 可执行文件或“DeepSeek / OCR”的旧文案。`extract.py` 用于读取 PDF 已有文本和页码标签，不是 OCR。预览只在本机渲染；正式识别会把所需页面的 PNG 图片发给 DeepSeek，可能产生 API 费用。`--dry-run` 只阻止写出 PDF，仍可能调用 API。
- **Windows 只发布免安装 ZIP**，保留 GUI 和 CLI 两个入口，不再生成安装程序。版本号、说明文档、程序目录和 ZIP 名称需一致。
- 单文件和目录模式都支持预览页面、显示原有书签、报告和结果；目录模式的列表编号应与日志“第 N/M 本”一致。选中、筛选 PDF 只改变显示，不应悄悄缩小整批处理范围。失败行、对应进度标题和失败日志以红色突出显示。
- 核对模式须以**可靠的印刷目录和页码映射**比较旧书签的标题、顺序、目标页及层级；一致则跳过，不一致且新结果可靠才替换。用户可选择只处理无书签的 PDF（只要有旧书签就直接跳过，不调用 API），或强制重建。非阿拉伯数字页码及无页码条目不计入新书签；`(1)`、`（１）` 等括号包裹的阿拉伯数字页码应识别为数字。
- **书签层级编号优先**：有章节编号的条目（第 N 章、Chapter N、§N、1.、1.1、1.1.1 等）按编号确定层级，不受模型缩进判断影响；书中有“第 N 章”时，§N、N. 是章下的节，N.M 归在 §N 之下。无编号条目按印刷目录缩进放置，并参照同页有编号条目校准。不要恢复“缩进优先、编号只作上限”的旧规则。
- 写出的书签最前面有一条指向第一页目录页的一级书签，中文书名为“目录”，其他书为“Contents”（报告中记为 `toc_bookmark`，不计入识别出的目录条目）。核对旧书签时忽略指向目录页的“目录/目次/Contents”一级书签，保证本工具的输出再次核对仍判为一致，原书签一致的 PDF 不会为补这条书签而改写。
- `needs_review` 表示结果不够可靠，`failed` 表示处理异常；两者均不应写出可能错位的新 PDF，更不能覆盖原件。`success` 需完成写入后重读校验。同盘覆盖可原子替换；跨盘覆盖需在程序目录备份、写回校验，失败时恢复。`dry_run`、`skipped`、`needs_review`、`failed` 保持原 PDF 不变。
- 报告、识别缓存、临时数据和加密密钥都放在程序目录 `data/`；未选择覆盖时，输出目录只放生成的书签 PDF，覆盖时输出目录不参与处理。GUI 的 DeepSeek Key 用当前 Windows 用户的 DPAPI 加密并保存在程序目录 `data/deepseek-api-key.dpapi`，除非用户手动删除，否则持续保存；CLI 从 `DEEPSEEK_API_KEY` 环境变量读取。密钥不可硬编码，也不可进入参数、日志、报告、测试夹具、仓库或发布包。不要在文档里记录用户曾提供的 Key。

## 代码入口

| 路径 | 用途 |
| --- | --- |
| `bookmarker/__main__.py` | `python -m bookmarker`、`single`/`batch` CLI、进度与报告；`--verify-existing`、`--skip-bookmarked`、`--replace-existing`、`--overwrite-original`、`--resume`、`--dry-run` 等参数。 |
| `bookmarker/app.py`、`bookmarker/gui.py` | 打包入口及 Tkinter GUI；GUI 在后台运行 CLI，避免阻塞界面。 |
| `bookmarker/gui_report.py` | GUI 的 JSONL 结果读取模块。 |
| `bookmarker/preview.py` | 本地扫描 PDF、读取旧书签与页面预览；预览不调用 DeepSeek。 |
| `bookmarker/deepseek.py` | PDFium 渲染、DeepSeek 请求、目录识别与识别缓存。 |
| `bookmarker/toc.py`、`bookmarker/extract.py`、`bookmarker/pipeline.py` | 目录清理和层级、PDF 已有文字与页码、页码映射、书签比较、安全写出与校验。 |
| `bookmarker/key_cache.py` | Windows DPAPI 密钥缓存。 |
| `tests/`、`packaging/` | 单元测试；PyInstaller onedir、免安装 ZIP、许可证与说明。 |
| `.github/workflows/windows-portable.yml` | 在 GitHub 的 Windows 服务器上测试、打包、检查 ZIP、试启动 CLI/GUI；推送 `v<版本>` 标签时上传同名 Release。 |

## 修改、验证和交付

1. 先运行 `git status --short`、`git diff`，确认当前分支与远端版本。保留别的会话或用户未提交的修改；只编辑本需求涉及的文件。代码行为以当前实现为准，必要时更新本文件的进度快照。
2. 本地验证优先用无需真实书籍、无需联网的自动化测试：`python -m pip install -r requirements-dev.txt`，再运行 `python -m unittest discover -s tests -v`。针对本次改动做相应 GUI/CLI 冒烟验证。不要把私有 PDF 书库或真实 DeepSeek 调用当作例行测试；如确需调用，只发送用户已授权的文档页面，并说明费用。
3. 完成实现和验证后只暂存本任务文件，检查 `git diff --cached --name-only` 与 `git diff --cached`，确认无 Key、私人 PDF、缓存、报告、打包产物及其他进行中的改动，再提交并推送。`.gitignore` 是辅助保护，不能代替暂存内容检查。**推送分支或创建 PR 后，GitHub Actions 工作流 `.github/workflows/windows-portable.yml` 会自动在 Windows 上测试、打包、检查 ZIP 并试启动 CLI/GUI**，ZIP 作为构建产物保留 30 天；推送后核对运行结果，失败须修复。普通推送不需要发布 Release，也不要手动上传 ZIP。
4. **只在用户明确要求时发布**，测试版也一样。平时的改动只提交、推送并由工作流检查，不改版本号、不创建 Release；多次改动可以攒到用户要求时一起发布。

   发布新版本由工作流完成，本地和云端都一样，只是触发方式不同。先同步 `bookmarker/__init__.py`、两份打包脚本和 README 中的版本号并推送提交，确认该提交的工作流通过后再触发发布：
   - 能推送标签时（例如在本地 Windows 上用 Codex），推送与 `bookmarker.__version__` 一致的 `v<版本>` 标签即可。
   - 不能推送标签时（Claude Code 云端会话推送标签会被 HTTP 403 拒绝），手动运行“Windows 免安装包”工作流（Actions 页面的 Run workflow，或通过 GitHub 接口触发），选择该分支并填写该标签。

   工作流会重新完成全部检查，通过后创建同名 Release 并上传 ZIP：标签不存在时在所选分支的当前提交上创建，已存在时重新构建该标签的提交；已有 Release 则替换其中的 ZIP。版本号含 `rc`、`a`、`b`、`dev` 的测试版自动标为预发布版，不要冒充正式稳定版。标签与版本号不一致时工作流会拒绝发布。若存在 `packaging/release-notes/v<版本>.md`，新建 Release 时用它作为发布说明（正式版应写），否则使用通用说明。GitHub 会把附件名中的中文替换成 `.`，所以 Release 附件使用英文名 `PDFBookmarker-Windows-Portable-<版本>.zip`，并附中文说明标签；压缩包内的程序文件夹仍为 `PDF书签工具`。发布后核对 Release 的 Assets。
5. **正式版只从 `main` 发布**：打正式版标签前，改动须已合并进 `main`，标签指向 `main` 上的提交。测试版可以先从开发分支发布供试用，确认后再合并。合并开发分支时使用普通合并（Create a merge commit），不要用 Squash 或 Rebase，否则已发布标签指向的提交不会出现在 `main` 的历史中。
6. 工作流不可用时才在本地打包：使用 Python 3.12 的干净虚拟环境并安装 `packaging/release-requirements.txt`，先跑测试和 `pip check`，再运行 `./packaging/build.ps1 -Python <虚拟环境的 python.exe>`。它只构建免安装 ZIP；`./packaging/package_portable.ps1 -Version <版本>` 仅用于重新打包已有 onedir。检查 ZIP 可解压、GUI/CLI 可启动、含 PDFium 与许可证，且不含 PDF、密钥、缓存、报告或旧 OCR 工具，再手动上传到同版本 Release，附件名同上。此环境未必安装 `gh`，不要把它当作必备工具。
7. **提交说明、PR 标题和 PR 说明一律使用中文。**

`dist/`、`tmp/`、书籍、结果和识别缓存均是本地文件，不随源码提交。若发布或推送遇到权限问题，保留已验证的文件和提交，明确报告失败步骤；不要泄露凭据或将密钥改存到仓库中。
