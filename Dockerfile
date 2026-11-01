FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev --extra ui
COPY config ./config
COPY data/generate.py ./data/generate.py
COPY evals ./evals
COPY ui ./ui
RUN uv run python data/generate.py --seed 42
EXPOSE 8000
CMD ["uv", "run", "uvicorn", "fraud_investigator.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
