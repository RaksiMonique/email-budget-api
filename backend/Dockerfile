FROM python:3.12-slim

WORKDIR /app

# install dependencies first (cached layer), then copy source
COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-cache-dir .

# alembic needed at runtime for Render's Pre-Deploy Command (alembic upgrade head)
COPY alembic.ini ./
COPY alembic ./alembic

# Render injects PORT; default for local docker runs.
# RUN_MIGRATIONS=true runs alembic at boot (free tier has no preDeployCommand);
# migration failure must abort the boot, not serve against a stale schema.
ENV PYTHONUNBUFFERED=1
CMD ["sh", "-c", "if [ \"$RUN_MIGRATIONS\" = \"true\" ]; then alembic upgrade head || exit 1; fi; exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
