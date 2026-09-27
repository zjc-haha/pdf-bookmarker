# PDF 书签工具 0.6.8（Windows 免安装版）

本版本仅用 DeepSeek 视觉模型识别 PDF 目录页。识别需要网络连接和 DeepSeek API Key。可处理单个 PDF，或递归处理整个文件夹。默认保留原 PDF；勾选“直接覆盖原 PDF”后，成功处理的文件会在原位置替换，报告与识别缓存仍写入所选输出文件夹。

今后只提供免安装 ZIP，不再制作安装版。

## 启动与首次试跑

1. 将 `PDF书签工具-免安装版-0.6.8.zip` **完整解压**。不要在压缩包内直接运行。
2. 打开解压后的 `PDF书签工具` 文件夹，双击 `PDF书签工具.exe`。
3. 选择单个 PDF 时，左侧列出该文件，中间显示首页，右侧展示现有书签。选择文件夹时，左侧会递归列出待处理的 PDF；点击某本书即可切换页面和书签预览。可在中间用前后按钮翻页、调整缩放比例或全屏查看。选中某本书只切换预览，批量处理仍会处理文件夹中的待处理 PDF。列表“序号”与日志中的 `[序号/总数]` 对应，筛选后编号也不变。界面默认勾选“按目录核对已有书签”，因此看起来已有完整章节书签的 PDF 也会列入待处理。“已有书签就跳过（不核对）”选项默认关闭；勾选后任何有至少一条书签的 PDF 都会快速跳过，无书签的 PDF 继续处理。
4. 选择输出文件夹。识别需要网络连接和 [DeepSeek API Key](https://platform.deepseek.com/api_keys)；在界面的密码输入框填写密钥，可用眼睛按钮切换显隐，也可预先设置 `DEEPSEEK_API_KEY` 环境变量。输入框失焦、开始处理或关闭窗口时，程序会把密钥用当前 Windows 用户的 DPAPI 加密，保存在 `%LOCALAPPDATA%\PDFBookmarker\deepseek-api-key.dpapi`；下次打开自动填入。清空输入框并移开焦点或关闭窗口可删除缓存。
5. 首次使用先勾选“仅分析，不生成 PDF”，检查输出目录的报告；确认无误后再正式处理。需要改写原文件时再勾选“直接覆盖原 PDF（仅成功时替换）”，单本和整批均适用。输出位置仍用于报告和缓存。

请保留 `PDF书签工具.exe`、`PDF书签命令行.exe` 和 `_internal` 文件夹的相对位置；移动时一起移动整个 `PDF书签工具` 文件夹。免安装版已包含 Python、PDF 处理库和 PDFium，适用于 64 位 Windows 10/11。当前构建未做代码签名，Windows 可能显示来源提示。

## 命令行

在解压后的 `PDF书签工具` 文件夹打开 PowerShell。命令行从 `DEEPSEEK_API_KEY` 环境变量读取密钥；下面的设置只对当前 PowerShell 会话生效：

```powershell
$env:DEEPSEEK_API_KEY = Read-Host "DeepSeek API Key"

# 单本 PDF 先试跑，再正式处理
.\PDF书签命令行.exe single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果" --dry-run
.\PDF书签命令行.exe single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果"

# 文件夹批量处理；--limit 只取前 5 本
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果" --dry-run --limit 5
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果"

# 可选：成功时覆盖原 PDF，--output 仍存放报告和缓存
.\PDF书签命令行.exe single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果" --overwrite-original
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果" --overwrite-original

# 核对已有书签与印刷目录；只有不一致且识别可靠时才替换
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果" --verify-existing --dry-run
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果" --verify-existing

# 快速跳过任何已有书签的 PDF；单本和批量均适用
.\PDF书签命令行.exe single "D:\PDF书库\一本书.pdf" --output "D:\PDF书签结果" --skip-bookmarked
.\PDF书签命令行.exe batch "D:\PDF书库" --output "D:\PDF书签结果" --skip-bookmarked
```

默认输出时，生成的 PDF 以 `_deepseek_bookmarked.pdf` 结尾。启用覆盖时，成功处理的 PDF 保留原路径和文件名；先在原目录写临时文件，校验后才替换。`--dry-run` 不会替换文件。`bookmarker-summary.csv` 和 `bookmarker-report.jsonl` 分别提供汇总与详细识别结果。

图形界面默认勾选“按目录核对已有书签”；命令行可加 `--verify-existing` 启用。程序从印刷目录生成可信的章节标题、目标 PDF 页码及层级，与原书签比较：一致则跳过；缺失、多余、标题、目标页或层级不一致时，会在新输出 PDF 中替换旧书签。目录中成对括号包裹的阿拉伯数字页码（如 `(1)`、`（１）`）按数字页码解析；罗马数字 `i`、`ii`、`iii` 等非数字页码和无页码项目会被跳过且不生成书签。保留条目的页码映射不可靠时，报告标记 `needs_review`，不会写出可能错位的结果。默认保留原 PDF；启用覆盖时，失败或需人工复核的文件保持原样。扫描质量和目录结构不同，不能保证每本书都能成功识别。大量纯数字页码等低质量书签仍会触发重新识别；`--replace-existing` 或界面的“强制替换已有书签”用于不比较、直接重做已有合格书签的情况。

若勾选“已有书签就跳过（不核对）”或加 `--skip-bookmarked`，只要文件有至少一条已有书签就记为 `skipped`，不调用 DeepSeek、不核对质量，也不修改原 PDF。此选项优先于目录核对、强制替换和原文件覆盖；没有书签的 PDF 仍正常处理。该选项默认关闭，单本和批量均适用。

## 图片上传与费用

工具会把需要识别的 PDF 页面渲染为图片发送到 DeepSeek API；PDF 原文件不会作为 PDF 上传。请只处理允许提交给该服务的文档。DeepSeek 按输入和输出 token 计费，图片也计入输入 token。图形界面默认核对现有书签，即使书签看起来完整也可能调用 API。`--dry-run` 只是不写入 PDF；首次识别仍会调用 API，可能产生费用。详见 DeepSeek 官方的[价格表](https://api-docs.deepseek.com/quick_start/pricing/)和[视觉输入说明](https://api-docs.deepseek.com/guides/vision/)。

0.6.6 新增“已有书签就跳过（不核对）”。0.6.5 将 `(1)`、`（１）` 等成对括号包裹的阿拉伯数字页码按数字处理，避免目录解析因这类页码失败；数字页码仍须可靠映射。0.6.4 会忽略目录中罗马数字等非数字页码的条目，避免单凭罗马数字页码无法校准而使整本书进入 `needs_review`。0.6.3 在直接覆盖原 PDF 时，对高质量旧书签与新目录几乎没有共同标题的情况改为 `needs_review`，避免误覆盖；报告会显示共同标题数，日志不再重复输出已知的 FontBBox 警告，汇总 CSV 也会在每本处理后更新。0.6.2 修复无页码前言导致整本书失败；0.6.1 修复编号书签层级漂移。当前免安装版为 0.6.8。第三方组件及许可条款见 `THIRD_PARTY_NOTICES.md` 和 `licenses` 文件夹。
