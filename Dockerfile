FROM python:3.13-slim

COPY --from=ghcr.io/astral-sh/uv:0.12.1 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

EXPOSE 8000

CMD ["uv", "run", "uvicorn", "fplquant.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
