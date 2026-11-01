FROM python:3.9-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN useradd --create-home --uid 10001 keel \
    && mkdir -p /app/data \
    && chown -R keel:keel /app

COPY requirements-runtime.txt pyproject.toml README.md ./
COPY app ./app

RUN pip install --no-cache-dir -r requirements-runtime.txt \
    && pip install --no-cache-dir --no-deps .

RUN chown -R keel:keel /app
USER keel

EXPOSE 8082
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082"]
