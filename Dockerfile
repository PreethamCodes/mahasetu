FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORT=8000
WORKDIR /app
COPY requirements.txt requirements-db.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY mahasetu_core ./mahasetu_core
COPY contracts ./contracts
COPY db ./db
COPY alembic.ini ./
USER nobody
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/health' % os.environ['PORT'], timeout=2)"
CMD ["sh", "-c", "exec uvicorn mahasetu_core.main:app --host 0.0.0.0 --port \"$PORT\""]
