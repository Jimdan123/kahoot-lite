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
#
# --timeout 1800 (30 min, gunicorn's default is 30s): the AI pipeline's
# extract_text() node has no page cap — a large PDF (~100 pages) does many
# sequential CPU-bound page-to-image renders plus one vision API call per
# detected figure/formula/diagram, all inside a single background greenlet.
# gevent's cooperative scheduling only yields on I/O, so sustained CPU work
# there can starve the event loop long enough to miss gunicorn's own
# heartbeat to its arbiter — which kills and restarts the worker, wiping
# the in-memory job registry (app/ai/jobs.py) and leaving the processing
# page polling a job that no longer exists. This raises the ceiling so a
# legitimately slow large-document run has room to actually finish instead
# of getting killed mid-job. Not a fix for unbounded processing time itself
# (no page cap was added — see extract_text's module docstring) — a
# sufficiently huge/figure-heavy PDF could still exceed even this.
CMD ["sh", "-c", "gunicorn --worker-class geventwebsocket.gunicorn.workers.GeventWebSocketWorker -w 1 --timeout 1800 --bind 0.0.0.0:$PORT wsgi:app"]
