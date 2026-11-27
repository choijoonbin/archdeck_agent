FROM python:3.14-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN pip install --no-cache-dir uv==0.13.10 && uv sync --frozen --no-dev
RUN useradd --uid 10001 --create-home agent
USER agent
EXPOSE 4010
CMD [".venv/bin/uvicorn", "archdeck_agent.main:app", "--host", "0.0.0.0", "--port", "4010"]
