# Recall Validator Docker Image
# Build: docker build -t recall-validator .
# Run: docker compose up -d  (or: docker run …)

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1

# Install system dependencies
RUN apt-get update && apt-get install -y \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (better layer caching)
COPY pyproject.toml requirements.lock ./

# requirements.lock is the hash-preserving export of uv.lock (no dev/project).
RUN pip install --no-cache-dir --require-hashes --only-binary=:all: --no-deps -r requirements.lock

# Copy application code
COPY . .

# Runtime modules are imported directly from /app; no editable install needed.

# Create non-root user for security
RUN useradd -m -u 1000 validator && \
    chown -R validator:validator /app

# Environment configuration
ENV NETWORK=finney
ENV NETUID=31
ENV WALLET_NAME=validator
ENV HOTKEY_NAME=default
ENV LOG_LEVEL=INFO

# Run the validator
CMD ["python", "main.py"]
