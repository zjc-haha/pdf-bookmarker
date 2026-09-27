# PDF 书签工具：给后续会话的工作说明

本文件适用于整个仓库。开始新需求前，先读本文件和 `README.md`，再核对当前代码、`git status`、远端提交及 Release；下面的版本状态只是 **2026-09-27 的快照**。

## 当前进度

- 公开仓库：<https://github.com/zjc-haha/pdf-bookmarker>。目前已发布的稳定版是 **v0.6.8**，该标签的源码基线提交为 `ba7a50e`；编写本文件前 `origin/main` 也指向该提交。对应免安装包在该版本的 GitHub Release。
- 工作区另有 **0.6.9rc1 本地测试界面**：源码、README、测试和打包脚本存在尚未提交的改动，`bookmarker/gui_report.py`、`tests/test_gui_report.py` 是新文件；本地 `dist/portable/PDF书签工具-免安装版-0.6.9rc1.zip` 仅是测试包，**尚未作为 GitHub Release 发布**。不要把它写成远端已发布版本，也不要在处理别的需求时覆盖或顺带提交这些工作。后续会话应以当时的 `git status` 和远端状态重新判断。
- v0.6.8 已实现：DeepSeek 视觉识别印刷目录、PDF 页码校准、单文件与目录批处理、已有书签核对与替换、快速跳过任何已有书签的 PDF、安全覆盖原文件、报告和 Windows 免安装包。0.6.9rc1 的界面改动说明见当前工作区 `README.md` 的“当前测试界面”一节；在提交、验收、发布前一律视为进行中。

## 已确定的产品要求

- **识别只用 DeepSeek**。不要恢复本地 OCR 引擎、OCR 模式选择、外部 OCR 可执行文件或“DeepSeek / OCR”的旧文案。`extract.py` 用于读取 PDF 已有文本和页码标签，不是 OCR。预览只在本机渲染；正式识别会把所需页面的 PNG 图片发给 DeepSeek，可能产生 API 费用。`--dry-run` 只阻止写出 PDF，仍可能调用 API。
- **Windows 只发布免安装 ZIP**，保留 GUI 和 CLI 两个入口，不再生成安装程序。版本号、说明文档、程序目录和 ZIP 名称需一致。
- 单文件和目录模式都支持预览页面、显示原有书签、报告和结果；目录模式的列表编号应与日志 `[序号/总数]` 一致。选中、筛选 PDF 只改变显示，不应悄悄缩小整批处理范围。0.6.9rc1 正在改进完整文件清单、预计操作、结果/日志切换和处理设置窗口；修改这些功能时先核对当前未提交代码。
- 核对模式须以**可靠的印刷目录和页码映射**比较旧书签的标题、顺序、目标页及层级；一致则跳过，不一致且新结果可靠才替换。用户可选择只处理无书签的 PDF（只要有旧书签就直接跳过，不调用 API），或强制重建。非阿拉伯数字页码及无页码条目不计入新书签；`(1)`、`（１）` 等括号包裹的阿拉伯数字页码应识别为数字。
- `needs_review` 表示结果不够可靠，`failed` 表示处理异常；两者均不应写出可能错位的新 PDF，更不能覆盖原件。`success` 需完成写入后重读校验。启用“直接覆盖原 PDF”时，只在成功校验后原子替换；`dry_run`、`skipped`、`needs_review`、`failed` 保持原 PDF 不变。报告与识别缓存仍放在输出目录。
- GUI 的 DeepSeek Key 由当前 Windows 用户的 DPAPI 加密缓存到 `%LOCALAPPDATA%\PDFBookmarker\deepseek-api-key.dpapi`；CLI 从 `DEEPSEEK_API_KEY` 环境变量读取。密钥不可硬编码，也不可进入参数、日志、报告、测试夹具、仓库或发布包。不要在文档里记录用户曾提供的 Key。

## 代码入口

| 路径 | 用途 |
| --- | --- |
| `bookmarker/__main__.py` | `python -m bookmarker`、`single`/`batch` CLI、进度与报告；`--verify-existing`、`--skip-bookmarked`、`--replace-existing`、`--overwrite-original`、`--resume`、`--dry-run` 等参数。 |
| `bookmarker/app.py`、`bookmarker/gui.py` | 打包入口及 Tkinter GUI；GUI 在后台运行 CLI，避免阻塞界面。 |
| `bookmarker/gui_report.py` | 0.6.9rc1 进行中的 JSONL 结果读取模块，目前是未提交文件。 |
| `bookmarker/preview.py` | 本地扫描 PDF、读取旧书签与页面预览；预览不调用 DeepSeek。 |
| `bookmarker/deepseek.py` | PDFium 渲染、DeepSeek 请求、目录识别与识别缓存。 |
| `bookmarker/toc.py`、`bookmarker/extract.py`、`bookmarker/pipeline.py` | 目录清理和层级、PDF 已有文字与页码、页码映射、书签比较、安全写出与校验。 |
| `bookmarker/key_cache.py` | Windows DPAPI 密钥缓存。 |
| `tests/`、`packaging/` | 单元测试；PyInstaller onedir、免安装 ZIP、许可证与说明。 |

## 修改、验证和交付

1. 先运行 `git status --short`、`git diff`，确认当前分支与远端版本。保留别的会话或用户未提交的修改；只编辑本需求涉及的文件。代码行为以当前实现为准，必要时更新本文件的进度快照。
2. 本地验证优先用无需真实书籍、无需联网的自动化测试：`python -m pip install -r requirements-dev.txt`，再运行 `python -m unittest discover -s tests -v`。针对本次改动做相应 GUI/CLI 冒烟验证。不要把私有 PDF 书库或真实 DeepSeek 调用当作例行测试；如确需调用，只发送用户已授权的文档页面，并说明费用。
3. 用户明确要求**推送源码**时，完成实现和验证后只暂存本任务文件，检查 `git diff --cached --name-only` 与 `git diff --cached`，确认无 Key、私人 PDF、缓存、报告、打包产物及其他进行中的改动，再提交并推送。`.gitignore` 是辅助保护，不能代替暂存内容检查。未完成的本地测试版不要冒充正式发布版。
4. 用户要求**新免安装版或 GitHub Release**时，使用 Python 3.12 的干净虚拟环境并安装 `packaging/release-requirements.txt`，先跑测试和 `pip check`，再运行 `./packaging/build.ps1 -Python <虚拟环境的 python.exe>`。它只构建免安装 ZIP；`./packaging/package_portable.ps1 -Version <版本>` 仅用于重新打包已有 onedir。同步核对 `bookmarker/__init__.py`、两份打包脚本、README、便携说明中的版本信息；检查 ZIP 可解压、GUI/CLI 可启动、含 PDFium 与许可证，且不含 PDF、密钥、缓存、报告或旧 OCR 工具。通过后推送对应提交和标签，再在同版本 GitHub Release 上传 ZIP，并核对下载链接与资源。此环境未必安装 `gh`，不要把它当作必备工具。

`dist/`、`tmp/`、书籍、结果和识别缓存均是本地文件，不随源码提交。若发布或推送遇到权限问题，保留已验证的文件和提交，明确报告失败步骤；不要泄露凭据或将密钥改存到仓库中。
