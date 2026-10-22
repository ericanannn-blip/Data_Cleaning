# syntax=docker/dockerfile:1
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 APP_DATA_DIR=/data
ARG WITH_MEDIA=0
ARG WITH_SCIENCE=0
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr tesseract-ocr-eng tesseract-ocr-chi-sim tesseract-ocr-chi-tra \
    && if [ "$WITH_MEDIA" = "1" ]; then apt-get install -y --no-install-recommends ffmpeg; fi \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements*.txt ./
RUN --mount=type=secret,id=proxy_ca \
    if [ -f /run/secrets/proxy_ca ]; then \
      if [ "$WITH_SCIENCE" = "1" ]; then PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements-science-lock.txt; \
      else PIP_CERT=/run/secrets/proxy_ca pip install --no-cache-dir -r requirements-lock.txt; fi; \
    elif [ "$WITH_SCIENCE" = "1" ]; then pip install --no-cache-dir -r requirements-science-lock.txt; \
    else pip install --no-cache-dir -r requirements-lock.txt; fi
COPY backend ./backend
COPY dat-file-reader/dat_reader.py ./dat-file-reader/dat_reader.py
RUN chmod -R a=rX /app \
    && groupadd --gid 10001 app && useradd --uid 10001 --gid 10001 --no-create-home app \
    && mkdir -p /data && chown app:app /data
ENV APP_ENV=production OCR_LANGUAGES=eng+chi_sim+chi_tra
USER 10001:10001
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('http://127.0.0.1:'+os.getenv('PORT','8000')+'/health',timeout=4)"
CMD ["python", "-m", "backend"]
