FROM python:3.12.7-slim-bookworm

# Tesseract OCR binary + English/Vietnamese language data. tesseract-ocr on
# Debian only *recommends* tesseract-ocr-eng, and --no-install-recommends
# below would silently drop it — list it explicitly rather than assuming.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
        tesseract-ocr-vie \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# gevent's monkey.patch_all() must run before any other import — that
# ordering constraint lives inside wsgi.py itself, so this just needs to
# invoke the same gunicorn command as Procfile/render.yaml. sh -c is
# required for $PORT to expand (exec-form CMD does no shell substitution).
CMD ["sh", "-c", "gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --bind 0.0.0.0:$PORT wsgi:app"]
