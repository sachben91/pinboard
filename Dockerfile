FROM python:3.11-slim

WORKDIR /app

# Install rclone and curl
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl unzip ca-certificates && \
    curl -fsSL https://rclone.org/install.sh | bash && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY . .

CMD ["bash", "scripts/start_bot.sh"]
