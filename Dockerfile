FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8080 \
    STATE_DIR=/app/state

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --disable-pip-version-check -r requirements.txt

COPY . ./
RUN mkdir -p /app/state

EXPOSE 8080
VOLUME ["/app/state"]

HEALTHCHECK --interval=30s --timeout=8s --start-period=90s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:'+os.environ.get('PORT','8080')+'/api/health',timeout=5).read()" || exit 1

CMD ["python", "-X", "utf8", "server.py"]
