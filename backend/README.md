# Resilience backend

FastAPI API, PostgreSQL database, background worker, and Nostr relay.

## Requirements

- Python 3.11 or newer
- Docker Engine with Docker Compose v2
- `jq` (used by the example commands)

On Ubuntu, install the local tools with:

```bash
sudo apt update
sudo apt install python3-venv jq docker.io docker-compose-v2
sudo usermod -aG docker "$USER"
```

Log out and back in after adding your user to the `docker` group.

## First-time setup

From the repository root:

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env
```

Create and sign a development client configuration. This generates a throwaway development key;
never use `--dev` or its generated key in production.

```bash
KEY=$(python scripts/sign_config.py --dev | grep '^PLATFORM_PUBKEY=')
sed -i "s/^PLATFORM_PUBKEY=.*/$KEY/" .env
```

The Compose file publishes PostgreSQL on host port `5433` so it can coexist with a PostgreSQL
installation on `5432`. Set the URL used by local commands:

```bash
sed -i 's#@localhost:[0-9]*/resilience$#@localhost:5433/resilience#' .env
```

The API and worker containers use the Compose service name `db` and the internal database port
`5432`; Compose configures this for them.

## Run the full stack

From `backend/`:

```bash
docker compose up --build
```

This starts PostgreSQL, the Nostr relay, the API, and the worker. The API applies database
migrations on startup. Wait until the API container reports healthy. The services are available
locally at:

- API: `http://localhost:8000`
- Relay: `ws://localhost:7777`
- PostgreSQL: `localhost:5433`

Check the API from another terminal:

```bash
curl -fsS http://localhost:8000/healthz
curl -fsS http://localhost:8000/v1/config | jq
```

Stop the stack with `Ctrl+C`, or from another terminal run:

```bash
docker compose down
```

To also delete local database and relay data, run `docker compose down -v`. This permanently
deletes the Compose volumes.

## Run the API with reload

Use this when changing Python code. Keep PostgreSQL in Docker and run the API and worker directly
in terminal sessions. Start the database from `backend/`:

```bash
docker compose up -d db
```

The `.env` file must point to `localhost:5433`, as set above. In the first terminal:

```bash
source .venv/bin/activate
alembic upgrade head
uvicorn app.main:app --reload --no-access-log
```

In a second terminal, from `backend/`:

```bash
source .venv/bin/activate
python -m app.worker
```

The API and worker load `.env` when they start. Restart them after changing `.env`.

To run the relay as well, start the full Compose stack. Stop its `api` and `worker` containers first
if you are running those processes directly:

```bash
docker compose up -d db relay
docker compose stop api worker
```

## Run the checks

Tests require a PostgreSQL database named `resilience_test`. They drop and recreate its `public`
schema, so do not point `TEST_DATABASE_URL` at a database with data you need.

Start the Docker database and create the test database if it does not already exist:

```bash
docker compose up -d db
docker compose exec db psql -U resilience -tc \
  "select 1 from pg_database where datname='resilience_test'" | grep -q 1 \
  || docker compose exec db psql -U resilience -c "create database resilience_test"
```

In the same terminal where you will run pytest:

```bash
source .venv/bin/activate
PORT=$(docker compose port db 5432 | cut -d: -f2)
export TEST_DATABASE_URL="postgresql+psycopg://resilience:resilience@localhost:${PORT}/resilience_test"
ruff check .
ruff format --check .
pytest -v
```

## Test signing helpers

`scripts/nostr_dev.py` provides development-only helpers for creating Nostr test keys, NIP-98
headers, NIP-05 test files, signed counsellor rosters, and signed counsellor profiles. Use only
throwaway secret keys with this script. Run `python scripts/nostr_dev.py --help` for usage.

For example, to give a counsellor on an approved organisation's roster a profile:

```bash
C1_SEC=$(printf '01%.0s' $(seq 32))
C1=$(python scripts/nostr_dev.py pubkey --sec $C1_SEC)
python scripts/nostr_dev.py profile --sec $C1_SEC --name "Counsellor Grace" \
  --specialty "Trauma support" --specialty "Legal aid" --language English --language Kiswahili \
  --response-time "Usually replies within a few hours" > /tmp/p1.json
curl -s -X PUT -H "Content-Type: application/json" --data @/tmp/p1.json \
  "http://localhost:8000/v1/orgs/$ORG_ID/counsellors/$C1/profile" | jq
curl -s "http://localhost:8000/v1/orgs/$ORG_ID/counsellors" | jq '.counsellors[] | {status, profile}'
```

Protected API requests use a NIP-98 kind `27235` event. The `u` tag must contain the full public
request URL, the `method` tag must match the HTTP method, and write requests must include the SHA-256
hash of the exact request body in a `payload` tag. Send the base64-encoded event in
`Authorization: Nostr <event>`.

## Configuration

| Setting | Purpose |
|---|---|
| `DATABASE_URL` | SQLAlchemy PostgreSQL URL |
| `PUBLIC_API_BASE` | Public API URL used to validate NIP-98 requests |
| `CORS_ORIGINS` | Comma-separated list of exact web client origins; `*` is rejected. The example allows the Vite web client on `http://localhost:5173` |
| `PLATFORM_PUBKEY` | Hex public key used to verify the signed client configuration |
| `SIGNED_CONFIG_PATH` | Path to the signed client configuration event |
| `ADMIN_PUBKEYS` | Comma-separated hex public keys allowed to administer organisations |
| `NIP05_TIMEOUT_SECONDS` | Timeout for organisation website checks |
| `NIP05_DEV_BASE_URL` | Local NIP-05 test-site override; only used when `APP_ENV` is `dev` or `test` |
| `LIGHTNING_NETWORK` | BOLT11 invoice network: `bc` (mainnet), `tb` (testnet), `bcrt` (regtest), or `tbs` (signet) |

Do not commit `.env`, private keys, or generated signed configuration files. The example settings
are for local development only.
