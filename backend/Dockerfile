FROM python:3.12-slim

WORKDIR /app

# Install system runtimes needed by Skill scripts (Node.js, npm, Ruby …)
RUN apt-get update && \
    apt-get install -y --no-install-recommends nodejs npm ruby && \
    rm -rf /var/lib/apt/lists/*

# Install dependencies first (layer cache)
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source
COPY backend/ ./backend/

# skills/ and kernel/ are provided via Docker volumes at runtime
RUN mkdir -p skills kernel

CMD ["uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
