# PDF 书签工具

从书籍 PDF 的印刷目录识别章节标题与页码，校准书上页码和 PDF 页面位置，再写入可点击的书签。可处理单个 PDF，也可递归处理整个文件夹。工具使用 DeepSeek 视觉模型识别目录页。默认保留原文件；勾选“直接覆盖原 PDF”后，识别和写入成功的文件会在原位置替换。当前测试构建把报告、识别缓存和加密密钥保存在程序的 `data/` 文件夹，所选输出目录只放生成的书签 PDF。

## Windows 版本 0.6.8

0.6.8 免安装 ZIP 的发布入口：[GitHub Releases](https://github.com/zjc-haha/pdf-bookmarker/releases/tag/v0.6.8)。发布后请从对应版本的 Assets 下载 ZIP，完整解压后打开 `PDF书签工具` 文件夹，双击 `PDF书签工具.exe`。请保留整个文件夹，不要单独移动 EXE，也不要在压缩包内直接运行。今后只打包免安装 ZIP，不再制作安装版。构建方法见文末。

0.6.8 仅使用 DeepSeek 识别，提供单文件和文件夹批量入口。打包版包含 Python、PDF 依赖和 PDFium。图形界面采用三栏布局：左侧列出 PDF，中间预览所选 PDF 的页面并提供翻页、缩放和全屏查看，右侧展示现有书签。默认勾选的“按目录核对已有书签”会把看起来完整的书签也纳入处理：开始处理后识别印刷目录并校准 PDF 页码，一致则跳过；发现章节缺失、标题、目标页或层级不一致时，在新输出 PDF 中替换原书签。“已有书签就跳过（不核对）”选项默认不勾选；勾选后，只要 PDF 有至少一条书签，就在调用 DeepSeek 前跳过，不检查书签是否正确，单本和批量均适用。目录或页码映射不可靠时只标记 `needs_review`，不生成可能错位的 PDF。图形界面的 DeepSeek API Key 输入后会为当前 Windows 用户加密缓存，下次打开自动填入；清空输入框即可删除缓存。文件列表的序号与处理日志的 `[序号/总数]` 对应，筛选文件时序号保持不变。勾选“直接覆盖原 PDF”适用于单本或整批，输出位置仍用于报告和缓存。首次使用建议勾选“仅分析，不生成 PDF”检查报告，再正式处理。当前构建未做代码签名，Windows 可能显示来源提示。

0.6.6 新增“已有书签就跳过（不核对）”：有至少一条旧书签的 PDF 直接记为 `skipped`，不识别目录、不核对书签质量、不修改 PDF，也不调用 DeepSeek；即使同时启用“按目录核对已有书签”或“强制替换已有书签”，仍优先跳过。没有书签的 PDF 正常处理。命令行使用 `--skip-bookmarked`。

0.6.5 将目录中成对括号包裹的阿拉伯数字页码（如 `(1)`、`（１）`）按数字页码解析，避免这类目录条目因“页码无效”而使整本书处理失败。没有阿拉伯数字页码的条目（如罗马数字 `i`、`ii`、`iii` 和无页码项目）仍会跳过，不计入新书签。保留条目的数字页码仍须可靠映射到 PDF 页面；映射不可靠时仍不会写入 PDF。

0.6.3 对“直接覆盖原 PDF”增加保护：高质量旧书签与新目录几乎没有共同标题时，标记为 `needs_review` 并保持原文件不变；核对后仍可勾选“强制替换已有书签”。报告增加共同标题数；已知的 PDF FontBBox 字体元数据警告不会再刷屏，其他错误保留。批处理每完成一本就更新汇总 CSV，停止后仍可查看已完成部分。

0.6.2 会略过目录中没有印刷页码的前言等条目，避免整本书因这些无目标页的项目失败；对章、节等需要定位的条目仍严格检查页码。

0.6.1 会依据章、节、小节编号校正模型偶发的层级漂移，例如把 `1.10` 放回第 1 章、把 `2.8.2` 放回 `2.8` 下。对 0.6.0 已处理的书籍，即使启用“续跑”，新版也会重新生成书签；DeepSeek 识别缓存仍可复用。请从原 PDF 重新运行，以修复此前生成的文件。

## 从源码运行

需要 Windows 10/11 和 Python 3.11 或更新版本。安装 Python 依赖：

```powershell
cd .\pdf-bookmarker
python -m pip install -r requirements.txt
```

若当前 PowerShell 已位于 `pdf-bookmarker` 目录，无需再执行上面的 `cd`。

DeepSeek 识别需要网络连接和 [DeepSeek API Key](https://platform.deepseek.com/api_keys)。命令行从环境变量 `DEEPSEEK_API_KEY` 读取密钥。例如在当前 PowerShell 会话中输入：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key"
```

### 当前测试界面（0.6.9rc8 测试版）

双击 [启动PDF批量书签工具.bat](启动PDF批量书签工具.bat) 或运行 `python -m bookmarker.gui` 打开界面。在本工作区，若存在同级的 `..\books`，会自动作为输入；默认输出是软件目录内的 `output`，不会自动在来源旁新建目录。选择单个 PDF 或文件夹后，左侧列出**全部**找到的 PDF，包括预计跳过和无法读取的文件；单击预览、双击用本机默认阅读器打开原 PDF。筛选或选中一本书只改变清单与预览，运行整批时仍检查整个来源文件夹。顶部显示预计识别、跳过和异常数量。中间可翻页、缩放、全屏预览，右侧可切换现有书签、当次识别结果和中文日志；选中书签可跳到目标 PDF 页。

0.6.9rc8 Windows 免安装测试包见 [GitHub 预发布版](https://github.com/zjc-haha/pdf-bookmarker/releases/tag/v0.6.9rc8) 的 Assets，由 GitHub Actions 在 Windows 上构建并通过测试和启动检查。完整解压后运行其中的 `PDF书签工具.exe`。这版统一了界面样式：标签页、下拉框、表格和滚动条改为扁平样式，复选框和单选框改为圆角图标，中文提示按字符正常换行；路径输入框内置清除按钮，“处理设置”入口并入运行范围信息栏，底部状态与报告提示合为一栏。同时修复了写出书签 PDF 时丢失页码标签（如 i、ii、1、2）、页面版式、语言和阅读器设置的问题。0.6.9rc7 起为主要操作和预览工具栏使用 Tabler Icons Outline 图标。

运行范围信息栏右侧的“处理设置”在独立小窗口中打开，分为“DeepSeek API Key”“处理规则”“运行方式”三组，每个选项下附有说明。可选互斥的“核对并修正（推荐）”“只处理无书签”“强制重建”。强制重建时还可记录旧书签差异；“仅分析”“续跑”和“直接覆盖原 PDF”是独立选项。切换规则会使用已扫描的 PDF 元数据更新预计操作，不会重新扫描文件；若来源文件在预检后变化，开始前会刷新清单，请检查新计划后再次开始。仅分析不生成 PDF，但仍可能调用 DeepSeek 并产生费用；预览只在本机完成。

书签层级依据印刷目录中的缩进与版式：同一视觉层级的条目即使没有章节编号，也作为同级书签。例如某章下面与 `5.1` 并列的 `Bibliography`、`Exercises` 会归在该章下；书末独立列出的参考文献仍可作为顶层条目。新版仅重新识别目录页，已缓存的目录定位和正文页码结果可复用。

点击顶部“API Key”可打开设置并输入密钥，眼睛按钮可切换显隐。密钥在输入框失焦、开始处理或关闭窗口时保存到项目根目录的 `data/deepseek-api-key.dpapi`，由 Windows DPAPI 为当前用户加密。免安装版保存在 EXE 同级的 `data/` 中；首次读取时会迁移旧版 `%LOCALAPPDATA%\PDFBookmarker\deepseek-api-key.dpapi`，迁移成功后删除旧文件。清空输入框不会删除已保存密钥，失焦时会恢复显示；只有手动删除软件目录中的 `data/deepseek-api-key.dpapi` 才会清除它。处理时文件列表逐本显示状态；日志将“第 1/20 本”等进度单独加粗显示，失败的文件行和失败日志以红色突出显示，界面文字统一使用微软雅黑。完成后可按“需复核”或“失败”筛选，并在右侧查看识别条目、原因、目录页和生成的 PDF。命令行仍从 `DEEPSEEK_API_KEY` 环境变量读取密钥。

每组输入和输出对应 `data/jobs/<任务编号>/` 中的一份汇总、详细报告和识别缓存。当前测试构建的 `--output` 目录只保存生成的书签 PDF；旧版留在输出目录中的报告和缓存会迁到软件目录，供续跑继续使用。“仅分析”时报告仍写入 `data/jobs/`。完成后界面显示本次 API 返回的输入、输出及总 Token，并注明未返回用量的响应次数。直接覆盖原 PDF 时，输出位置不参与处理，也不影响续跑记录；成功后原文件在原位置替换。写入临时文件和跨盘覆盖的原件备份都放在软件目录 `data/temp/`。跨盘覆盖会先备份、再写回并校验；如遇意外断电或强制终止，重新启动软件会检查恢复记录。跨盘写回无法做到原子替换，停机至下次启动之间原文件可能暂时不可读。

命令行示例：

```powershell
# 处理单个 PDF；输出文件名自动加书签后缀
python -m bookmarker single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果" --dry-run
python -m bookmarker single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果"

# 先分析少量书籍
python -m bookmarker batch ..\books --output .\output --dry-run --limit 5

# 查看报告后正式处理
python -m bookmarker batch ..\books --output .\output

# 可选：成功时直接替换原 PDF；无需指定输出位置
python -m bookmarker single "D:\PDF书库\一本书.pdf" --overwrite-original
python -m bookmarker batch ..\books --overwrite-original

# 中断后继续，或强制重做已有合格书签的 PDF
python -m bookmarker batch ..\books --output .\output --resume
python -m bookmarker batch ..\books --output .\output --replace-existing

# 先核对已有书签与印刷目录；只有不一致且识别可靠时才替换
python -m bookmarker batch ..\books --output .\output --verify-existing --dry-run
python -m bookmarker batch ..\books --output .\output --verify-existing

# 快速跳过任何已有书签的 PDF；单本和批量均适用
python -m bookmarker single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果" --skip-bookmarked
python -m bookmarker batch ..\books --output .\output --skip-bookmarked
```

单文件处理允许输出文件夹与原 PDF 所在目录相同。默认在输出文件名后添加后缀；`--overwrite-original` 则在新 PDF 通过页数和书签校验后替换原文件，忽略输出文件夹。`--dry-run` 不替换文件。默认输出的批量模式下，书签 PDF 输出文件夹须与来源文件夹分开。

## 图片上传与费用

工具会把识别所需的 PDF 页面渲染为图片，并发送到 DeepSeek API；PDF 原文件不会作为 PDF 上传。请只处理允许提交给该服务的文档。API 按输入和输出 token 计费，图片也计入输入 token。图形界面默认核对现有书签，即使书签看起来完整也可能调用 API。`--dry-run` 只是不写入书签 PDF；首次识别时仍会调用 API，可能产生费用，因此建议先配合 `--limit` 小批量试跑。费用和模型能力以 [DeepSeek 官方价格表](https://api-docs.deepseek.com/quick_start/pricing/) 为准；图片格式、大小和请求限制见 [视觉输入文档](https://api-docs.deepseek.com/guides/vision/)。

## 结果与校准

默认输出目录中只保存生成的 PDF，文件名以 `_deepseek_bookmarked.pdf` 结尾。启用“直接覆盖原 PDF”时，成功处理的 PDF 保留原文件名和位置，输出目录被忽略。汇总报告 `bookmarker-summary.csv`、详细报告 `bookmarker-report.jsonl` 和识别缓存保存在 `data/jobs/<任务编号>/`；`data/` 位于源码项目根目录，免安装版位于 EXE 同级。汇总报告适合用表格软件查看状态、条目数量、偏移量和警告；详细报告记录目录、正文页码锚点及映射结果。移动免安装版时，请连同 `data/` 文件夹一起移动以保留密钥、报告和续跑数据。

工具先识别目录页，将 `(1)`、`（１）` 等成对括号包裹的阿拉伯数字页码解析为数字，再跳过罗马数字等非数字页码或无页码的条目；随后读取正文页眉或页脚的印刷页码，计算 `PDF 页序 − 印刷页码`。保留条目的数字页码须有可靠的映射才会自动写入。书中插入未编号页面导致偏移量变化时，工具会尝试分段映射。写入时保留原书的页码标签、页面版式和阅读器设置；写入后会重新打开输出 PDF，检查页数、书签数量和页码标签。识别结果仍可能有误，请在首次试跑后核对报告和生成的书签。

- `success`：已写入并验证输出 PDF。
- `dry_run`：仅分析，识别结果在报告中。
- `skipped`：核对后现有书签与印刷目录一致；若关闭核对，已有较完整的章节书签也会直接跳过；启用“已有书签就跳过（不核对）”时，只要有至少一条书签就会跳过。
- `needs_review`：目录、页码或偏移量不够可靠，未生成可能错位的 PDF。
- `failed`：处理异常，原因在报告中。

当前源码界面默认选“核对并修正”；命令行使用 `--verify-existing` 启用同样的核对。核对包含目录条目的标题、目标 PDF 页码及层级，也能发现缺失或多余的条目。印刷目录和页码映射都可靠时，一致的 PDF 跳过，不一致的 PDF 在新输出中替换原书签。核对失败会给出 `needs_review`，不写出可能错位的结果。关闭核对时，原有书签很少但有用的 PDF 会保留旧书签，并将新书签放在“自动识别目录”下面；大量纯数字页码等低质量条目仍会尝试替换。`--replace-existing` 或源码界面的“强制重建”用于直接重做已有合格书签。默认保留原 PDF；启用直接覆盖时，仅对成功生成并校验的 PDF 原子替换，失败或需人工复核的文件保持原样。扫描质量和目录结构不同，不能保证每本书都能成功识别。

`--skip-bookmarked` 或当前源码界面的“只处理无书签”适合只想处理尚无书签的文件。启用时，有至少一条已有书签就直接 `skipped`，不调用 DeepSeek；无书签的 PDF 继续按常规流程处理。

## 旧版

0.6.6 增加“已有书签就跳过（不核对）”；0.6.5 支持括号包裹的阿拉伯数字目录页码；0.6.4 起跳过罗马数字等非阿拉伯数字页码的目录条目；0.6.3 加强原文件覆盖保护并改进报告。更早的版本逐步加入页面预览、书签核对与 DeepSeek 识别。旧版构建文件没有随源码上传；新用户请使用 0.6.8 免安装版。

## 工程目录与开发验证

- `bookmarker/`：批处理、目录识别、页码校准和图形界面源码。
- `tests/`：自动化测试。
- `packaging/`：Windows 构建脚本、配置和第三方许可证。
- `data/`：加密密钥、按任务划分的报告和识别缓存；不提交到 Git。
- `dist/`：本地构建的程序目录和免安装 ZIP；构建产物通过 GitHub Releases 发布，不随源码提交。

本工作区中的 `books/` 测试书库和 `bookmarked/` 处理结果位于工程目录外，与 `pdf-bookmarker/` 同级。运行测试：

```powershell
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
```

构建脚本位于 [packaging/build.ps1](packaging/build.ps1)。运行 `./packaging/build.ps1` 可从源码生成免安装 ZIP；只需重新打包已有程序目录时可运行 [packaging/package_portable.ps1](packaging/package_portable.ps1)。构建需要 Python 3.12 虚拟环境，并安装 `packaging/release-requirements.txt` 中列出的依赖。

也可以交给 GitHub Actions：[.github/workflows/windows-portable.yml](.github/workflows/windows-portable.yml) 在每次推送分支或创建 PR 时，于 Windows 上运行测试、打包、检查 ZIP 内容并试启动命令行和图形界面，ZIP 作为构建产物保存 30 天；推送与 `bookmarker.__version__` 一致的 `v<版本>` 标签时，还会把 ZIP 上传到同名 GitHub Release，测试版自动标为预发布。也可以在 Actions 页面手动运行该工作流并填写 `v<版本>`：标签不存在时，全部检查通过后会在所选分支的当前提交上创建标签和 Release。第三方组件及其许可证见 [packaging/THIRD_PARTY_NOTICES.md](packaging/THIRD_PARTY_NOTICES.md) 和 [packaging/licenses](packaging/licenses)。
