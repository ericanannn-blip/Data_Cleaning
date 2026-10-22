# 下载后在本地启动

这是完整源码包，包含前端、后端、SQLite 存储、读取器、测试和发布配置。
不包含云端虚拟环境、已上传原件、现有数据库或访问令牌。
在本地重新创建虚拟环境，避免直接复制不能跨电脑运行的 `.venv`。

## 快速启动

1. 将 ZIP 解压到可写的目录，进入其中的 `Data_Cleaning` 文件夹。
2. 安装 **Python 3.12**：<https://www.python.org/downloads/>。
   Windows 安装时勾选 **Add Python to PATH**。
3. 启动：
   - **Windows**：双击 `start_windows.bat`。
   - **macOS / Linux**：在此文件夹打开终端，执行 `bash start_macos_linux.sh`。
4. 首次启动会创建 `.venv` 并联网安装锁定版本依赖；以后的启动会复用该环境。
   出现 Uvicorn 的启动成功日志后，用浏览器打开 <http://127.0.0.1:8000>。
   API 文档是 <http://127.0.0.1:8000/docs>。

保持启动窗口打开。按 `Ctrl+C` 停止；重新运行同一启动脚本即可继续使用。
本地启动仅监听 `127.0.0.1`，默认无访问令牌；正式部署请按主 README 设置鉴权。
脚本不自动读取 `.env` 文件；需要自定义时，在运行前设置环境变量。

上传的原件和 SQLite 数据保存在 **`data/`**；停止、重启不会自动删除。
备份、移动或升级程序时请保留该目录，优先使用 README 中的在线备份命令。
此源码包不含此前上传的 `test.zip`，也不含针对它的真实数据测试结果。

## 可选读取组件

文本、表格、PDF 原生文字和 Office 的核心依赖由启动脚本安装。
图片文字及扫描 PDF 的 OCR 还需要本机安装 **Tesseract** 和对应语言模型。
缺少组件时仍可上传并保留原件，读取结果会明确显示待安装状态。

- **Windows**：安装方式见 [Tesseract 安装说明](https://github.com/tesseract-ocr/tesseract/wiki)。
  安装时选择英文、简体中文、繁体中文语言数据，将安装目录加入 PATH，再重新打开终端。
- **macOS**：使用 Homebrew 执行 `brew install tesseract`；中文语言模型可用
  `brew install tesseract-lang` 安装，完整语言包体积较大。
- **Ubuntu / Debian**：执行
  `sudo apt install tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra`。

中文 OCR 安装完语言模型后，在启动前设置语言：

Windows 命令提示符：

```bat
set OCR_LANGUAGES=eng+chi_sim+chi_tra
start_windows.bat
```

macOS / Linux：

```sh
OCR_LANGUAGES=eng+chi_sim+chi_tra bash start_macos_linux.sh
```

已上传文件可通过 API 的 `POST /files/{id}/reprocess` 重新解析。

读取 NPY、Parquet、HDF5 时，在启动脚本后增加 `--science`：

```bat
start_windows.bat --science
```

```sh
bash start_macos_linux.sh --science
```

这些依赖额外下载、按需加载，不在默认轻量环境中安装。
已安装的扩展不会因下一次省略 `--science` 而自动卸载。
音视频结构读取需要另行安装 FFmpeg，并确保 `ffprobe` 位于 PATH。
完整覆盖范围与质量边界见 `docs/readers.md`，不能保证未知、损坏或加密文件一定可读。

## 常见问题与手动启动

- **8000 端口被占用**：在启动脚本后加 `--port 8001`，改访问
  <http://127.0.0.1:8001>。
- **依赖安装失败**：检查网络后再次启动；用 `--reinstall` 强制重新检查依赖。
- **Linux 提示缺少 ensurepip**：安装系统对应的 `python3.12-venv` 包。
- **升级或更换电脑**：只迁移源码和数据，重新创建 `.venv`；不要拷贝旧虚拟环境。

也可以手动使用虚拟环境。在解压后的项目目录执行：

Windows：

```bat
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-lock.txt
.venv\Scripts\python.exe -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

macOS / Linux：

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
.venv/bin/python -m uvicorn backend.main:app --host 127.0.0.1 --port 8000
```

无需安装测试用的 Playwright 或 Docker 就能用本地网页。
源码包需要联网安装 Python 依赖，不是离线安装包；Windows 和 macOS 的启动脚本
已提供，但本次打包验收在 Linux / Python 3.12 中执行。
