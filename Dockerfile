# The app image (0007): Python 3.12 with uv. By default it runs `make check`, which needs
# the postgres service from docker-compose.yml.
FROM python:3.12-slim@sha256:191083cdf6c7bda8c76158983163ead5d5cf7b77cf3033e805bb0f5296f51b4b AS base

RUN apt-get update \
    && apt-get install --yes --no-install-recommends make \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

FROM base AS formal-tools
ARG TARGETARCH
RUN <<'SH'
set -eu
case "$TARGETARCH" in
    amd64) arch=x64; sha=adb5a2364baa51de1ef91bb9911f5a61d24b045fe1d6647cb8050272a3a8ee75 ;;
    arm64) arch=aarch64; sha=aae834297a87736869745be7c1fca3207ea9167c5824f41c88b0ebb2e3ccb9b1 ;;
    *) echo "No pinned Temurin JRE for $TARGETARCH" >&2; exit 1 ;;
esac
url="https://github.com/adoptium/temurin17-binaries/releases/download/jdk-17.0.19%2B10/OpenJDK17U-jre_${arch}_linux_hotspot_17.0.19_10.tar.gz"
python3 - "$url" /tmp/java.tar.gz <<'PY'
import shutil
import sys
from http.client import HTTPException
from ssl import SSLError
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

url, archive_path = sys.argv[1:]
for attempt in range(1, 4):
    try:
        with urlopen(url, timeout=60) as response, open(archive_path, "wb") as archive:
            shutil.copyfileobj(response, archive)
    except (ConnectionError, TimeoutError, HTTPException, SSLError, URLError) as error:
        permanent = isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code != 429
        if attempt == 3 or permanent:
            raise
        print(f"Temurin download attempt {attempt} failed: {error}. Retrying.", file=sys.stderr)
        sleep(attempt)
    else:
        break
PY
echo "$sha  /tmp/java.tar.gz" | sha256sum --check
mkdir -p /opt/java
tar -xzf /tmp/java.tar.gz --strip-components=1 -C /opt/java
SH

COPY Makefile ./
COPY scripts/tla_tools.py scripts/tla_tools.py
# Keep both TLC licence notices inside the verified, unmodified jar.
RUN make tla-setup

FROM base
RUN pip install --no-cache-dir uv==0.13.8 \
    && useradd --create-home --user-group --uid 10001 fivefold \
    && install -d -o fivefold -g fivefold /app /var/lib/fivefold

# Use the image's Python 3.12; never download another.
ENV UV_PYTHON_DOWNLOADS=never
USER fivefold

# Install the locked dependencies first, so code changes reuse this layer.
COPY --chown=fivefold:fivefold pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project

COPY --chown=fivefold:fivefold scripts/temporal_cli.py scripts/temporal_cli.py
COPY --chown=fivefold:fivefold src/fivefold/_temporal_cli.py src/fivefold/_temporal_cli.py
RUN PYTHONPATH=src python3 scripts/temporal_cli.py install

COPY --from=formal-tools /opt/java /opt/java
COPY --from=formal-tools --chown=fivefold:fivefold /app/.tla/tla2tools.jar .tla/tla2tools.jar
ENV JAVA_HOME=/opt/java
ENV PATH=/opt/java/bin:$PATH

COPY --chown=fivefold:fivefold . .
RUN uv sync --locked

CMD ["make", "check"]
