# Data_Cleaning · DAT Lab

本仓库包含在线私有站点的 Worker 源码，以及保留的本地 Python 后端。

本次同步：2026-10-22，来自已发布站点源码提交 `e6b5e7f4c63dce917604fb96ad11ea08d947a907`。

## 新版在线功能

- 8 MiB 分块上传，最多两个文件并行，支持暂停、重试、断点续传与 SHA-256 校验。
- 文件、结构 Schema、分类规则三个可切换工作区；文件详情按需打开，上传队列切换页面后保留。
- 按文件生成 Draft 2020-12 schema，附带筛选规则、判断结果、证据和覆盖范围。
- 保留上传原字节，支持原名映射及按检测格式下载；同名文件使用独立 ID。
- 使用 D1 持久化记录与上传会话，R2 保存原件及 multipart 分块；数据库迁移采用追加方式。

完整接口和限制见 [分块上传说明](docs/resumable-upload-api.md)，界面操作见 [工作区说明](docs/UI_UPGRADE.md)。

## 在线版本验证

```sh
npm ci
npm run build
npm run verify
node scripts/test-site.mjs
node scripts/test-ui.mjs
python scripts/validate-site-schemas.py
```

最后一项需 Python 环境已安装 `jsonschema`。本地实际存储接口测试验证了 64 MiB + 17 字节文件的下载哈希，以及 201 文件批次分页；15 个生成 schema 和样本通过校验。新版已在应用内浏览器实测导入原名映射、上传6个合成文件（含20 MiB文件）、保留上传队列、搜索及手机端详情布局。

配置单文件上限为 256 MiB，不能把该配置当作已完成相同规模的实测。在线内容检查最多 64 KiB；PDF、图片、压缩包目前提供元数据 schema 和原件下载，没有执行正文提取、OCR、解压或自动语义主题分类。分类规则页展示现有词表及真实待分类状态。本轮仅调查不接大模型的分类方法，见[规则分类方案](docs/model-free-classification.md)，没有执行真实文件分类。大 JSON 不做完整结构推断。

仓库不包含真实 DAT、原名映射、私人语料、凭据、运行数据库或测试上传结果。公开源码仓库不改变在线站点的私有访问权限。

---

## 本地 Python 后端与原有说明

# 持久化脏数据接入与内容 schema

在原 `codex/dat-web-demo` 分支的 FastAPI、SQLite 和 DAT reader 上继续实现。
文件按真实内容识别，`.dat`、原文件名和客户端 MIME 都不决定读取方式。
附件中的文字只作为数据，不执行指令、脚本、宏、pickle 或用户提供的 SQL。

**本地 Python 后端状态：提供容器、持久卷、鉴权和备份恢复配置，未部署公网服务。在线 Worker 的状态与限制见上方说明。**
最初上传的 `test.zip` 超过平台 `download_file` 的 32 MiB 传输上限，未成功下载。
仓库中的验证使用明确标记的合成数据；真实附件的全量验收仍待可访问的数据。
平台传输上限与本程序的上传 API 无关。

## 运行

下载源码包后在本地电脑运行，优先查看 [LOCAL_START.md](LOCAL_START.md)。
Windows 使用 `start_windows.bat`；macOS / Linux 使用 `bash start_macos_linux.sh`。
启动脚本自动创建本地虚拟环境，只监听 `127.0.0.1`。

Python 3.12 的本地开发环境：

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-lock.txt
python -m pip install -r requirements-dev.txt
python -m backend
```

本地入口 `http://localhost:8000`，API 文档 `/docs`。网页接受任意扩展名，
保留原有搜索、分页、详情和拖放上传；详情中显示内容读取状态与三套 schema。
本地图片 OCR 需要安装 Tesseract 和对应语言模型。容器包含英文、简体中文、
繁体中文模型；本地默认仅请求 `eng`，中文数据应设置 `OCR_LANGUAGES`。

发布容器：先设置 `API_TOKEN`（至少 32 个字符，不要提交 Git），运行：

```sh
docker compose up --build -d
```

默认只监听本机 `127.0.0.1:8000`。对外提供服务时，在前方配置 HTTPS 和相应
代理超时。生产模式没有有效令牌会拒绝启动。API 使用 `Authorization: Bearer …`；
网页提供令牌输入框，不写入浏览器存储。`/health`、静态网页和只含公开配置的
`/config` 无需令牌，文件元数据、内容、schema、下载和上传均受鉴权保护。

运行一个实例、一个 Uvicorn worker，持久卷同时保存数据库和原文件。SQLite WAL
需要支持锁和 fsync 的本地磁盘，不使用网络文件系统共享多个副本。Railway 仍可
使用 `railway.json`，需先挂载 `/data` 卷并配置令牌；配置文件不会自动创建卷。

旧 root 用户写入的 Docker 卷升级为 UID 10001 前，应停服、备份，再调整卷中
应用目录所有权。新卷由镜像初始化；宿主机 bind mount 需允许 UID 10001 写入。

## API：不设上传文件数量上限

服务端使用 `python-multipart` 回调解析，当前只持有一个文件的写入状态。请求每
64 KiB 交给磁盘写入器，完成一个文件即提交数据库和解析任务。不调用会默认
限制 1,000 个文件并缓存整张表单的 `request.form()`，也不积攒整批结果到内存。

`MAX_FILES` 已移除；传入旧环境变量不会重新产生数量限制。字节配额默认均为
`0`（不限制），可按实际磁盘设置。实际可用磁盘、代理超时和客户端资源仍有限。

| 接口 | 用途 |
| --- | --- |
| `POST /files` | multipart 中重复 `files` 字段，任意数量、任意扩展名 |
| `POST /files?filename=x.dat` | `application/octet-stream` 原始流，适合大文件和工具调用 |
| `POST /batches` | 创建可持续追加的批次 |
| `POST /files?batch_id=…&filename=x.dat` | 向已有批次追加原始流或 multipart |
| `GET /batches/{id}?after=…&limit=200` | 批次结果按游标分页 |
| `GET /files?limit=100&offset=0` | 原有文件索引、数量统计和分页 |
| `GET /files/{id}` | 原有格式识别元数据 |
| `GET /files/{id}/analysis` | 持久解析任务状态、内容样本、读取结果 |
| `GET /files/{id}/schemas` | 三套内容 schema 和抽样证据 |
| `GET /schemas` | 按内容结构指纹汇总的 schema 目录 |
| `POST /files/{id}/reprocess` | 清除当前内容缓存并重新排队，HTTP 202 |
| `GET /files/{id}/download` | 下载原始字节，强制 attachment，不内联执行 |

multipart 示例（为每个文件增加一个 `files` 字段）：

```sh
curl -H "Authorization: Bearer $API_TOKEN" \
  -F "files=@sample1.dat" -F "files=@sample2.png" http://localhost:8000/files
```

大文件示例，curl 从磁盘流式读取：

```sh
curl -H "Authorization: Bearer $API_TOKEN" \
  -H "Content-Type: application/octet-stream" \
  --data-binary @sample.dat "http://localhost:8000/files?filename=sample.dat"
```

响应保留 `files`、`succeeded`、`failed`，增加 `batch_id`、`state`、
`results_truncated`、`next_cursor`。`files` 每页最多 200 条，这只是响应分页，
不是上传限制。`succeeded/failed` 是批次累计计数；使用已有批次时也包含之前
请求。原始流响应另有 `uploaded_file`，明确指出这次请求的文件。

断连或缺少结束边界时，已完成的文件继续保留，未完成文件清理，自动创建的
批次标记 `interrupted`。批次号未知时可从 `/files` 确认已保存文件再决定重试。
接口不保证重试请求只写入一次；重复上传保留不同记录、独立原件，同内容的
解析通过 SHA-256 缓存复用。

## 读取工具与覆盖范围

GitHub 调研、工具选择和局限见 [docs/readers.md](docs/readers.md)。
默认安装 PDF、图片、OCR、Office 和文本解析组件。各大组件只在匹配文件的
子进程中导入，解析完成后进程退出；没有常驻 pandas、深度学习模型或 JVM。

| 内容 | 当前读取路径 |
| --- | --- |
| PDF | pypdf 文本/页数；无文本页用 PDFium 渲染和 Tesseract OCR |
| PNG/JPEG/TIFF/GIF/WebP/BMP/ICO 等 | Pillow 元数据、首帧 OCR；支持单字节 XOR 图片还原 |
| CSV/TSV/分隔表/数值矩阵 | stdlib csv、编码识别、多位置抽样、脏行与重复列检查 |
| JSON/JSONL | ijson 流式记录抽样；大 JSON 只验证采到的前缀 |
| XML/HTML/RTF | defusedxml/HTMLParser/striprtf；禁用实体与脚本执行 |
| DOCX/PPTX/ODF | 按 ZIP 内真实 XML 结构识别，抽取文字 |
| XLSX | openpyxl 只读模式，保留公式字符串，不执行公式或外链 |
| GZIP/BZIP2/XZ/ZLIB | 在字节预算内读取解压前缀，不声称验证未读的尾部 |
| ZIP | 抽样成员清单；显式 `import --expand` 读取安全成员并长期保存 |
| SQLite | immutable、只读、禁用 trusted_schema；表结构与行抽样 |
| NPY/Parquet/HDF5 | 可选 science 依赖；内存映射/批读取/跨位置标量抽样 |
| WAV/MP3/MP4/OGG/FLAC/AVI/Matroska/FLV | 可选 ffprobe 技术元数据，不进行语音转写或视频语义理解 |
| 旧 Office、RAR/7z、其他格式 | 可选、由运营者配置的 Tika 服务兜底；未配置则保留并标记待适配 |

science 扩展：`pip install -r requirements-science-lock.txt`，或构建镜像时设置
`WITH_SCIENCE=1`；媒体组件设置 `WITH_MEDIA=1`。也可用相同环境变量运行 Compose。
启用新依赖或改变抽样策略会生成新的缓存键，已处理文件自动进入新的解析队列。
Tika 服务通过 `TIKA_URL` 显式指定，原件只在配置该服务且需要兜底时发送给它。

**不能保证任意未知二进制、已损坏或加密文件都能读出有效内容。**
所有成功上传的字节均可保存和下载；读取结果明确使用 `sampled`、`encrypted`、
`needs_ocr`、`needs_review`、`corrupt`、`dependency_missing`、`resource_limit`、`unsupported` 等状态。
图片 OCR 只能提取可识别文字，不理解图片语义；识别到文件头也不证明文件有效。
OCR 保存引擎的样本文字置信分数，均分低于 60 时标记复核；这个阈值是保守提示，
不是经过真实语料校准的准确率。
如果质量不足，应提高适当的预算、安装对应语言/格式组件或补充领域适配器，
不把空内容或二进制头当作有效读取结果。

## 三套内容 schema

原件始终保持原样；schema 仅为版本化候选，不自动清洗或强制执行约束。

1. `faithful`：源字段、样本中实际类型及混合类型，保留源数据含义。
2. `analytics`：规范列名、缺失值、数值/日期转换建议，保留 `001` 等标识符。
3. `semantic`：内容族和分析角色：标识符、时间、度量、维度、文本。

内容族包括 `tabular`、`document`、`media_asset`、`array`、`archive_inventory`、
`opaque`。不可读内容没有伪造的内容 schema。字段指纹按内容族、字段和类型
生成，与文件名和采样行数无关；相同结构可在 `/schemas` 汇总。

默认预算：64 KiB 内容样本、200 条记录、3 页 PDF。大表按首、中、尾三个字节
窗口取分散记录；PDF 取首、中、尾页；数组按跨数组位置取值。JSON 数组以
流式前缀取样；Excel 取首批工作表/行。这些差异都会记录在 `sampling` 中。
列级记录样本缺失率、类型、非空去重数量，报告重复行、宽度不一致和重复表头。
候选键仅标记为“样本内可能唯一”，不直接声明主键；必填字段默认为空。
抽样不能证明全量唯一性、完整性、尾部合法性或业务规则，必须保留这项边界。

解析缓存包含原文件 SHA-256、reader 版本、Python 依赖版本、原生读取器标识、抽样预算、OCR 语言和
兜底配置，重复文件无需重新大量阅读。示例见 [examples/content-schema.json](examples/content-schema.json)。

## 数据库、长期保存与恢复

SQLite `user_version=2` 在原 `files` 表上增加 `batches`、`batch_items`、`jobs`、
`analyses`，无删除或重写历史文件。WAL、`synchronous=FULL`、busy timeout、
外键、`BEGIN IMMEDIATE` 保护元数据与配额事务；原文件 flush/fsync、原子重命名
和目录 fsync 后再提交索引与任务。名称只作元数据，私有路径由 UUID 生成。

上传与解析分离。队列存储租约、尝试次数和令牌，过期租约可恢复，最多自动重试
三次，重新解析会使旧结果失效。默认每次解析限制 60 秒、Linux 地址空间
512 MiB、结果 2 MiB，并结束整个子进程组。Windows 只执行超时限制；生产使用
Linux 容器。这里的子进程隔离是资源控制，不是完整的操作系统安全沙箱。

没有自动删除已提交文件的策略，启动不删除原件或孤儿原件，只清理超过 24 小时
的临时 `.part` 文件。查询均分页，不在启动时装载所有文件 ID。永久保存仍需
真正的持久卷、磁盘容量管理和经验证的离线备份。

```sh
python -m backend.manage verify
python -m backend.manage backup /backup/unique-snapshot-directory
python -m backend.manage restore /backup/unique-snapshot-directory /new/data
```

备份使用 SQLite 在线 backup API，然后按快照引用复制原件并校验 SHA-256，
最后才写完成清单。目标必须为新目录，不覆盖已有备份。恢复验证清单、数据库
与所有原件后复制到新的数据目录；停服后再切换挂载，不覆盖正在运行的库。
不要只复制正在使用的 `app.db` 而漏掉 WAL 和原件。

## 实际语料测试

本地大 ZIP 不受聊天附件 32 MiB 限制：

```sh
python -m backend.manage import /path/test.zip --expand
python -m backend.manage drain
python -m backend.manage verify
python scripts/validate_corpus.py /path/test.zip --output test-results/corpus
```

`validate_corpus.py` 自己执行导入，因此不需要先手动 import；上面前三行是独立
操作工具示例。解压成员始终写入生成的私有路径，拒绝路径穿越、符号链接、
加密成员和超过展开字节预算的内容。无归档成员数量上限；stdlib ZipFile 的
中央目录仍有与成员数相关的内存开销。ZIP 原件和每个成功展开成员都长期保存。
目录输入接受全部常规文件，不局限于 `.dat`。成员 CRC 损坏或压缩方法不受支持时，
记录失败并继续其他成员；不把不完整的成员当作成功上传。

输出包含逐文件 `manifest.jsonl`、格式/读取状态/内容族统计、每种内容结构的
三套 schema、数据库完整性和逐文件校验。不可读文件不会静默丢失。原件存放于
`APP_DATA_DIR`，报告默认在 Git 忽略的 `test-results` 中。

## 验证

```sh
python -m unittest discover -s dat-file-reader -p 'test_*.py' -v
python -m unittest discover -s tests -p 'test_*.py' -v
python tests/browser_demo.py --screenshots test-results
docker build -t dat-ingestion:test .
python tests/docker_acceptance.py
```

核心回归、实际 HTTP/浏览器、容器重建和负载验证说明见
[docs/verification.md](docs/verification.md)。原始无第三方库 CLI 及其原有输出
保持兼容；[dat-file-reader/README.md](dat-file-reader/README.md) 说明它的独立行为。
其 64 MiB 整体解码和 archive 规则不等于新后端的上传限制。

配置项见 [.env.example](.env.example)，应用不会自动加载该文件。数据库、原件、
令牌、真实语料和测试输出不会提交到 Git。
