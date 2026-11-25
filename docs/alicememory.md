# Exercise the AliceMemory adapter locally

AliceMemory is the maintainer's memory platform. The optional exercise
seeds a synthetic purchase-order memory, reads it by ID, corrects its order
channel and checks both values in its revision history. The in-repo memory
supplies all scored F8 evidence. The optional exercise keeps its own
disposable AliceMemory instance and runs outside CI. [Record 0023](adr/0023-memory-correction.md)

AliceMemory v0.25.0 tightened `POST /v0/vnext/memories/correct` for
memories that wait for a person, and the exercise can still correct its
own committed memory.

## Provision a disposable instance

Use a fresh AliceMemory v0.25.0 checkout and Python 3.12. The following
commands run in Bash from that checkout. Use free local ports for PostgreSQL,
Redis and the API. If you change the ports, update the matching URLs.
The commands use the pinned tag's role bootstrap and local API entry
point. [AliceMemory local runtime](https://github.com/samrusani/AliceMemory/blob/v0.25.0/docs/alpha/local-runtime.md#commands)

```sh
uv venv --python 3.12
uv pip install --python .venv/bin/python -e .

f8_admin_password=$(openssl rand -hex 24)
f8_app_password=$(openssl rand -hex 24)
f8_user=$(.venv/bin/python -c 'from uuid import uuid4; print(uuid4())')
f8_state=$(mktemp -d)

docker run -d --rm --name fivefold-f8-alice-pg \
  -e POSTGRES_USER=alicebot_admin -e POSTGRES_PASSWORD="$f8_admin_password" \
  -e POSTGRES_DB=alicebot -e ALICEBOT_APP_USER=alicebot_app \
  -e ALICEBOT_APP_PASSWORD="$f8_app_password" \
  -v "$PWD/infra/postgres/init:/docker-entrypoint-initdb.d:ro" \
  -p 127.0.0.1:55432:5432 pgvector/pgvector:pg16
docker run -d --rm --name fivefold-f8-alice-redis \
  -p 127.0.0.1:56379:6379 redis:7-alpine

f8_alice_app="postgresql://alicebot_app:$f8_app_password@127.0.0.1:55432/alicebot"
f8_alice_admin="postgresql://alicebot_admin:$f8_admin_password@127.0.0.1:55432/alicebot"
f8_alice_env=(
  env -i "PATH=$PATH" "DATABASE_URL=$f8_alice_app"
  REDIS_URL=redis://127.0.0.1:56379/0 "ALICEBOT_AUTH_USER_ID=$f8_user"
  APP_ENV=development APP_HOST=127.0.0.1 APP_PORT=55877 APP_RELOAD=false
  APP_ACCESS_LOG=false "TASK_WORKSPACE_ROOT=$f8_state"
)
```

The clean environment supplies the local database and Redis URLs without
loading providers or keys from your usual shell. Keep this checkout free
of a `.env` file. The migration and user seed take the database admin URL.
The API and key command use the ordinary application role.
[Runtime settings](https://github.com/samrusani/AliceMemory/blob/v0.25.0/apps/api/src/alicebot_api/config.py#L219-L298) · [Embedding opt-in](https://github.com/samrusani/AliceMemory/blob/v0.25.0/apps/api/src/alicebot_api/vnext_embeddings.py#L675-L685)

After both services report ready, migrate, seed the synthetic user and
capture an unbound trusted local agent key. Keep shell tracing off while
handling the key. [Migrations](https://github.com/samrusani/AliceMemory/blob/v0.25.0/scripts/migrate.sh) · [User seed](https://github.com/samrusani/AliceMemory/blob/v0.25.0/scripts/seed_local_user.py) · [Agent keys](https://github.com/samrusani/AliceMemory/blob/v0.25.0/docs/alpha/agent-integration.md#authentication)

```sh
docker exec fivefold-f8-alice-pg pg_isready -U alicebot_admin -d alicebot
docker exec fivefold-f8-alice-redis redis-cli ping

"${f8_alice_env[@]}" env DATABASE_ADMIN_URL="$f8_alice_admin" ./scripts/migrate.sh
"${f8_alice_env[@]}" env DATABASE_ADMIN_URL="$f8_alice_admin" \
  .venv/bin/python scripts/seed_local_user.py

set +x
f8_key=$("${f8_alice_env[@]}" .venv/bin/alicebot agent keys create \
  --agent-id fivefold-f8 --profile trusted_local_agent \
  | .venv/bin/python -c 'import json, sys; print(json.load(sys.stdin)["raw_key"])')
"${f8_alice_env[@]}" .venv/bin/python -m alicebot_api.local_server &
f8_api_pid=$!
```

## Run the exercise and clean up

In the same shell, switch to the FiveFold checkout with its Python 3.12
environment ready. The exercise also runs from an installed FiveFold
package with this command. The URL must use a literal loopback address,
such as `127.0.0.1` or `[::1]`. Each exercise uses one fresh user and
one synthetic entry.

```sh
FIVEFOLD_ALICEMEMORY=1 \
FIVEFOLD_ALICEMEMORY_URL=http://127.0.0.1:55877 \
FIVEFOLD_ALICEMEMORY_USER_ID="$f8_user" \
FIVEFOLD_ALICEMEMORY_KEY="$f8_key" \
  uv run python -m fivefold.harness.alicememory
```

Success reports version 1 with `edi`, version 2 with `portal` and the
verified previous value. A refusal or incomplete response exits with
status 1. The adapter makes the HTTP calls. The operator owns the
instance, including cleanup of its PostgreSQL database, Redis and files.
Retiring the memory leaves its revision history, so remove the disposable
storage when the exercise is done.

```sh
kill "$f8_api_pid"
wait "$f8_api_pid" || true
docker stop fivefold-f8-alice-pg fivefold-f8-alice-redis
rm -r -- "$f8_state"
unset f8_key f8_alice_env f8_alice_app f8_alice_admin f8_app_password f8_admin_password
```

The containers use `--rm`, so stopping them removes their anonymous data
volumes. Run these commands in the shell that created the disposable
instance.
