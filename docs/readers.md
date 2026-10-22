# GitHub 读取工具调研与选择

2026-10-05 检查公开 GitHub 仓库及项目 README。仓库体积不等于安装体积或运行
内存；下表依据功能与加载方式选择，运行指标由本项目验收测量，不虚构单库 RSS。

| 文件内容 | 工具与来源 | 使用方式与取舍 |
| --- | --- | --- |
| PDF 原生文字 | [py-pdf/pypdf](https://github.com/py-pdf/pypdf) | 纯 Python；持有文件流，只对采样页抽取文字；无文本页转 OCR |
| 扫描 PDF | [pypdfium2-team/pypdfium2](https://github.com/pypdfium2-team/pypdfium2) | PDFium 原生渲染，在匹配子进程内加载；默认采 3 页，不整份渲染 |
| 多种图片 | [python-pillow/Pillow](https://github.com/python-pillow/Pillow) | 成熟编解码器，元数据与 OCR 输入；不自动使用庞大的视觉模型 |
| 图片文字 | [tesseract-ocr/tesseract](https://github.com/tesseract-ocr/tesseract) | 每次采样执行、随后退出；选择对应语言模型；复杂版式/手写字仍可能低质量 |
| JSON 流 | [isagalaev/ijson](https://github.com/isagalaev/ijson) | 流式解析记录，避免将整份大数组装入 Python |
| XLSX | [ericgazoni/openpyxl](https://github.com/ericgazoni/openpyxl) / [维护项目](https://foss.heptapod.net/openpyxl/openpyxl) | 历史 GitHub 仓库已非主维护位置；使用发布包只读模式，保留公式 |
| 安全 XML | [tiran/defusedxml](https://github.com/tiran/defusedxml) | 小型 Python 组件，拒绝 DTD/实体扩展；不采用不安全 XML 加载 |
| RTF | [joshy/striprtf](https://github.com/joshy/striprtf) | 简单文字提取，复杂版式不能保证还原 |
| 多文件 multipart | [Kludex/python-multipart](https://github.com/Kludex/python-multipart) | 回调流式解析，避免上传数量限制、整批缓存和整批结果增长 |
| 音视频结构 | [FFmpeg/FFmpeg](https://github.com/FFmpeg/FFmpeg) | 按需 ffprobe，读取流/时长/编码信息；不等同于转写内容或理解视频 |
| Parquet | [apache/arrow](https://github.com/apache/arrow) | 可选 pyarrow，分批读记录；安装体积较大，所以不进入默认轻量镜像 |
| NPY | [numpy/numpy](https://github.com/numpy/numpy) | 可选 numpy，内存映射，拒绝 pickle，对数组跨位置采样 |
| HDF5 | [h5py/h5py](https://github.com/h5py/h5py) | 可选 h5py，对数据集读取有限标量，而非整份数组 |
| 广泛格式兜底 | [apache/tika](https://github.com/apache/tika) | 官方声称识别/提取超过千种格式；不保证任意私有格式。仅显式配置服务后调用；JVM、OCR 及复杂解析器有独立资源成本 |

DOCX/PPTX/ODF 使用标准 ZIP 与 defusedxml，没有额外常驻 Office 进程。
CSV/TSV/数值矩阵、HTML、SQLite、GZIP/BZIP2/XZ/ZLIB 使用 Python 标准库。
这减少常驻依赖，但不会为损坏数据伪造正常结果。归档内容没有自动执行步骤。

核心解析包在本次 PyPI 下载中：pypdf wheel 约 0.33 MB、PDFium 约 3.7 MB、
Pillow 约 6.9 MB、openpyxl 约 0.25 MB、ijson 约 0.15 MB。可选 science 包中
numpy 约 16.6 MB、pyarrow 约 47.6 MB、h5py 约 5.1 MB。这些是下载体积，
不是解包体积，也不是运行时内存；Tesseract 和语言模型另外安装。

质量优先：Tesseract 无法可靠读取复杂手写、特殊语言或版式时，结果必须保留
待复核状态；当前保存 OCR 引擎词级分数，均分低于 60 提示复核。该分数不是
经校准的准确率。未来可接入更强 OCR/视觉组件，并通过真实语料验证。默认无大模型
常驻，但不是通过隐藏质量问题来换取较低内存。未安装扩展、加密、格式未知、
超预算和损坏都有独立状态；原文件始终可校验、下载和重新解析。
