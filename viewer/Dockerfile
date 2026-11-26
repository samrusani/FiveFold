# The viewer image (record 0012): the frontend, built with Node, served with the read-only
# API, on Python. `docker compose up viewer` runs it against the postgres service; build it
# from the repository root: `docker build -f viewer/Dockerfile .`.
FROM node:26.13.1-slim@sha256:abe7e5a82b61a8ef67b315b85497c9e65173387ccf2f4a029248184b8e020be5 AS frontend

WORKDIR /viewer
COPY viewer/package.json viewer/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY viewer/ ./
RUN npm run build

FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b

RUN pip install --no-cache-dir uv==0.13.8 \
    && useradd --create-home --user-group --uid 10001 fivefold \
    && install -d -o fivefold -g fivefold /app

# Use the image's Python 3.12; never download another.
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app
USER fivefold

# Install the locked dependencies first, so code changes reuse this layer.
COPY --chown=fivefold:fivefold pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY --chown=fivefold:fivefold . .
RUN uv sync --locked --no-dev
COPY --from=frontend /viewer/dist viewer/dist

EXPOSE 8300
CMD ["uv", "run", "--no-sync", "python", "-m", "fivefold.viewer", "--host", "0.0.0.0"]
