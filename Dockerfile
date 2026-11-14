# The app image (0007): Python 3.12 with uv. By default it runs `make check`, which needs
# the postgres service from docker-compose.yml.
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install --yes --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.13.8

# Use the image's Python 3.12; never download another.
ENV UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Install the locked dependencies first, so code changes reuse this layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY . .
RUN uv sync --locked

CMD ["make", "check"]
