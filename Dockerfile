FROM python:3.13-slim

# git fetches the function source. build-essential is there because a function's dependencies are
# installed at runtime, when a commit is deployed; drop it if every dependency you use ships wheels.
RUN apt-get update && \
    apt-get install -y --no-install-recommends git ca-certificates tzdata build-essential && \
    rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN uv pip install --system --no-cache -r /app/requirements.txt
COPY src /app/src

ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    MINIFAAS_DATA_DIR=/data

# 8080 serves the functions, 8081 the dashboard (no login: keep it on the LAN).
# There is deliberately no VOLUME: mount one on /data yourself. An anonymous volume would hide
# a missing mount, and the setup wizard warns when /data isn't one.
EXPOSE 8080 8081

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('MINIFAAS_DASHBOARD_PORT', '8081'), timeout=4)"

CMD ["python", "-m", "minifaas"]
