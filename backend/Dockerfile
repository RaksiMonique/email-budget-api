FROM python:3.12-slim

WORKDIR /app

# install dependencies first (cached layer), then copy source
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# alembic needed at runtime for Render's Pre-Deploy Command (alembic upgrade head)
COPY alembic.ini ./
COPY alembic ./alembic

# Render injects PORT; default for local docker runs
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
