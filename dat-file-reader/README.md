# DAT 文件阅读器（结果 schema 2.0）

使用 Python 3.10+，无需安装第三方库。程序按文件内容识别 `.dat` 的真实格式，可一次输入多个文件或目录；目录会递归扫描 `.dat`（不区分大小写）。文件逐个处理，结果逐行写入报告，不会把所有文件同时装入内存。

在 VS Code 的 Windows 终端运行：

```powershell
py .\dat_reader.py "D:\文件分类\06_敏感资料\ai" "D:\其他目录\一个文件.dat" -o "D:\dat_结果"
```

同一路径重复出现在多个输入中时，仅处理一次。单个文件可以不是 `.dat`；目录扫描只选 `.dat`。`--max-bytes 67108864` 可调整单个文件的内存解码及解压上限（默认 64 MiB）。超过上限的已识别图片、PDF、音视频等二进制文件仍按流复制；需要读取内容的超限文件记为 `skipped`。

## 输出目录

指定 `-o "D:\dat_结果"` 后会生成：

```text
D:\dat_结果\
  manifest.jsonl       每个扫描文件一行 JSON，UTF-8
  summary.json         数量、分类、格式统计和需要复核的样本
  files\               按类别存放的图片、PDF 等还原文件
  content\             按类别存放的文本或 JSON 内容，UTF-8
```

还原文件与内容先按 `image`、`document` 等类别，再按 `source_001`、`source_002` 等输入编号分目录保存，并保留原目录层级。同名文件不会互相覆盖。终端只输出报告路径和总数。再次使用同一输出目录时，报告会更新；报告列出的路径代表本次运行的结果。

`manifest.jsonl` 中的主要字段如下：

| 字段 | 含义 |
| --- | --- |
| `schema_version` | 结果结构版本，目前为 `2.0` |
| `source_path`、`source_index`、`relative_path` | 原文件路径、输入序号、相对路径 |
| `size_bytes`、`sha256` | 原文件字节数、校验值 |
| `status` | `ok`、`unsupported`、`skipped` 或 `error` |
| `category`、`format`、`mime` | 分类、真实格式、媒体类型 |
| `encoding`、`transform` | 文本编码、执行过的解码步骤 |
| `decoded_file` | 相对于输出目录的还原文件路径，可为空 |
| `content_file` | 相对于输出目录的文字或结构化内容路径，可为空 |
| `header_hex`、`error_code`、`error` | 未识别或失败时的诊断信息 |

分类包括 `image`、`document`、`text`、`audio`、`video`、`archive`、`database` 和 `unknown`。可把 `summary.json`、需要复核的 `manifest.jsonl` 行和对应 `.dat` 样本发来，依据 `schema_version` 迭代识别规则和报告字段。

当前可提取 UTF-8、带 BOM 的 UTF-16/UTF-32、GB18030 文本，JSON/XML/HTML，GZIP/BZIP2/XZ/ZLIB，ZIP，以及 DOCX/XLSX/PPTX 的部分文字内容。可识别并保存常见图片、音视频、PDF、SQLite、7z、RAR；固定单字节异或的 PNG/JPEG/GIF/WebP 图片可还原。PDF 目前保存为 PDF，不提取文字；扫描 PDF 的文字需要 OCR。未知二进制文件会标记为 `unsupported`，并记录前 32 字节供后续分析。

每次修改程序后可运行：

```powershell
py -m unittest -v test_dat_reader.py
```
