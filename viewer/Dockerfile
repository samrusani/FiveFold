# The viewer image (record 0012): the frontend, built with Node, served with the read-only
# API, on Python. `docker compose up viewer` runs it against the postgres service; build it
# from the repository root: `docker build -f viewer/Dockerfile .`.
FROM node:26.13.1-slim AS frontend

WORKDIR /viewer
COPY viewer/package.json viewer/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY viewer/ ./
RUN npm run build

FROM python:3.12-slim

RUN pip install --no-cache-dir uv==0.13.8

# Use the image's Python 3.12; never download another.
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install the locked dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY . .
RUN uv sync --locked --no-dev
COPY --from=frontend /viewer/dist viewer/dist

EXPOSE 8300
CMD ["uv", "run", "--no-sync", "python", "-m", "fivefold.viewer", "--host", "0.0.0.0"]
