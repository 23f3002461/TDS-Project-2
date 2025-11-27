# ---- Base image ----
FROM python:3.11-slim

# ---- System deps required for Playwright + PDFs ----
RUN apt-get update && apt-get install -y \
    wget curl unzip \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libxkbcommon0 libxcomposite1 libxdamage1 libxrandr2 \
    libgbm1 libasound2 libpangocairo-1.0-0 libpango-1.0-0 \
    libgtk-3-0 libx11-xcb1 libxcb1 libx11-6 \
    libxext6 libxfixes3 libxrender1 libxi6 \
    libffi-dev libssl-dev \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# ---- Python deps ----
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- Install Playwright browsers ----
RUN playwright install --with-deps chromium

# ---- Copy app ----
COPY . .

# Flask listens on port 8000
ENV PORT=8000
EXPOSE 8000

# ---- Start server ----
CMD exec python main.py

