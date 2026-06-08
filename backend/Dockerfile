FROM python:3.12-slim

WORKDIR /app

# Install system runtimes and common CLI tools needed by Skill scripts.
# Grouped by category:
#   - Language runtimes: nodejs, npm, ruby
#   - Database clients: sqlite3, mysql-client, postgresql-client
#   - Media processing: ffmpeg, imagemagick
#   - Document processing: poppler-utils (pdftotext), wkhtmltopdf (html→pdf)
#   - Network tools: curl, wget, jq
#   - Archive tools: zip, unzip
#   - Version control: git
#   - Infrastructure: openssh-server
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      nodejs npm ruby \
      sqlite3 default-mysql-client postgresql-client \
      ffmpeg imagemagick \
      poppler-utils wkhtmltopdf \
      curl wget jq \
      zip unzip \
      git \
      openssh-server \
    && rm -rf /var/lib/apt/lists/*

# 配置SSH服务
RUN mkdir -p /var/run/sshd && \
    echo "PermitRootLogin yes" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config

# 设置root密码为: skillfactory
RUN echo "root:123456" | chpasswd

# Pre-install Playwright's Chromium browser so skill scripts don't need to
# download it at runtime (downloading at runtime causes subprocess timeouts).
# The browser version is pinned to match skills/huashu-data-pro/package.json.
COPY skills/huashu-data-pro/package.json /tmp/playwright-install/package.json
RUN cd /tmp/playwright-install && \
    npm install --omit=dev && \
    npx playwright install chromium --with-deps && \
    cd / && rm -rf /tmp/playwright-install

# Install dependencies first (layer cache)
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

# Copy backend source
COPY backend/ ./backend/

# skills/ and kernel/ are provided via Docker volumes at runtime
RUN mkdir -p skills kernel

CMD ["bash", "-c", "/usr/sbin/sshd && tail -f /dev/null"]
#CMD ["bash", "-c", "/usr/sbin/sshd && uvicorn backend.main:app --host 0.0.0.0 --port 8000"]
