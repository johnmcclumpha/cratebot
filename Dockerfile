FROM python:3.12-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 1000 cratebot \
    && mkdir -p /app/data \
    && chown -R cratebot:cratebot /app
USER cratebot

VOLUME ["/app/data"]
ENV DATABASE_PATH=/app/data/cratebot.db

CMD ["cratebot"]
