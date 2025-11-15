# Dockerfile (minimal fix)
FROM python:3.11-slim

# Install system dependencies required by Playwright (browsers)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    wget \
    curl \
    gnupg \
    libnss3 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libx11-6 \
    libxcomposite1 \
    libxdamage1 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpangocairo-1.0-0 \
    libgtk-3-0 \
    libgdk-pixbuf-xlib-2.0-0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy requirements if present (create requirements.txt next to Dockerfile)
COPY requirements.txt /app/requirements.txt

RUN python -m pip install --upgrade pip
RUN pip install --no-cache-dir -r /app/requirements.txt

# Install Playwright browsers (with dependencies)
RUN python -m playwright install --with-deps

# Copy application code
COPY . /app

# Ensure entrypoint is executable
RUN chmod +x /app/check_prices.py || true

# Default command
CMD ["python", "check_prices.py"]
