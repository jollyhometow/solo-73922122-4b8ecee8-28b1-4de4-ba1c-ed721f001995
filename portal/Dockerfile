FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv

RUN groupadd --system app && \
    useradd --system --gid app --home-dir /srv --shell /usr/sbin/nologin app

COPY portal/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY portal ./portal

USER app

EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=5 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).read()"

CMD ["uvicorn", "portal.app:app", "--host", "0.0.0.0", "--port", "8080"]
