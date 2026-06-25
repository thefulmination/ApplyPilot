# Cloud apply-fleet worker. Railway builds this remotely (no local Docker needed).
# The base image's Chromium is launched by the worker (CHROME_PATH) and @playwright/mcp
# connects to it over CDP, so exact MCP/browser version-matching isn't required (CDP is
# stable). If this tag fails to build on Railway, bump to a current playwright image.
FROM mcr.microsoft.com/playwright:v1.52.0-noble
USER root

# Agent CLIs: Claude Code drives the apply; @playwright/mcp is the browser tool surface.
# Pinned + installed globally so the first lease pays no cold npx download.
RUN npm i -g @playwright/mcp@0.0.76 @anthropic-ai/claude-code

# Python + the worker runtime (the Playwright image is Ubuntu noble with node + browsers).
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-pip python3-venv curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Stable symlink to the image's Chromium (the chromium-<rev> dir is version-specific).
RUN ln -sf "$(ls /ms-playwright/chromium-*/chrome-linux/chrome | head -1)" /usr/bin/google-chrome

WORKDIR /app
COPY . /app
# applypilot (+ psycopg) for the worker; litellm[proxy] for the in-container DeepSeek proxy.
# python-jobspy is installed --no-deps (its metadata pins a numpy that breaks the resolver).
RUN pip3 install --no-cache-dir --break-system-packages -e . "litellm[proxy]" \
    && pip3 install --no-cache-dir --break-system-packages --no-deps python-jobspy || true

COPY deploy/litellm_config.yaml /app/litellm_config.yaml
COPY deploy/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV CLAUDE_PATH=/usr/local/bin/claude \
    CHROME_PATH=/usr/bin/google-chrome \
    APPLYPILOT_DIR=/data/applypilot \
    CHROME_WORKER_DIR=/tmp/chrome-workers \
    APPLY_WORKER_DIR=/tmp/apply-workers \
    APPLYPILOT_DB_PATH=/tmp/fleet_throwaway.db \
    ANTHROPIC_BASE_URL=http://127.0.0.1:4000 \
    APPLYPILOT_APPLY_MODEL=deepseek-chat \
    APPLYPILOT_PREFLIGHT_LIVENESS=0 \
    PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# Sealed in Railway, NOT baked into the image: DEEPSEEK_API_KEY, DATABASE_URL.
# PII (profile.json + resume.pdf) mounts from a Railway volume at /data/applypilot.
CMD ["/app/entrypoint.sh"]
