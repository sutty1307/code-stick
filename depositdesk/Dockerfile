FROM python:3.12-slim
RUN groupadd --system desk && useradd --system --gid desk --home-dir /app desk
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=desk:desk app.py providers.py wsgi.py gunicorn.conf.py backup.py sandbox_check.py connection_check.py ./
COPY --chown=desk:desk static/ ./static/
RUN mkdir -p /data && chown desk:desk /data && chmod 700 /data
ENV DEPOSITDESK_DATA_DIR=/data DEPOSITDESK_PROVIDER=simulator DEPOSITDESK_BIND=0.0.0.0:8765 PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
VOLUME ["/data"]
USER desk
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=4s --start-period=10s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/readyz',timeout=3).close()"
CMD ["gunicorn", "--config", "gunicorn.conf.py", "wsgi:application"]
