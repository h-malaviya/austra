FROM docker:29-cli AS docker_cli

FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils tesseract-ocr \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY --from=docker_cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker_cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/lib/docker/cli-plugins/docker-compose
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

ENV AUSTRA_WORKSPACE=/data/blank
EXPOSE 8000
CMD ["uv", "run", "--no-sync", "uvicorn", "austra.web:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
