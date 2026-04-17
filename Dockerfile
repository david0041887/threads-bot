FROM python:3.11-slim

# 安裝 Chromium 所需系統依賴
RUN apt-get update && apt-get install -y \
    chromium \
    chromium-driver \
    fonts-noto-cjk \
    --no-install-recommends && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 讓 Playwright 使用系統的 Chromium
ENV PLAYWRIGHT_BROWSERS_PATH=/usr/bin
ENV PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

RUN playwright install chromium --with-deps || true

COPY . .

CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000}
