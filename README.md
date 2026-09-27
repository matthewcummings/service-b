# service-b

A deliberately tiny FastAPI + Postgres CRUD service, one of two identical services
(`service-a`, `service-b`) used to demonstrate per-branch preview environments. The
interesting parts are the infrastructure concerns: two DB auth modes (IAM / password),
migrations under an advisory lock, seeding, copying `main`'s data into a preview, and CI.

The code is identical in both service repos; they differ only by environment variables
(`SERVICE_NAME`, `PATH_PREFIX`, ...).

**Data ownership:** this service owns its own logical database (`service_b`) and its own
Alembic history. No other service reads its tables; they would go through this HTTP API.
Preview envs get their own logical DB on the same Aurora cluster, named
`<db>__<group>` (e.g. `service_b__checkout`), with its own IAM-only DB user.

## Endpoints

All routes are mounted under `PATH_PREFIX` (e.g. `/a`), because the ALB forwards the path
unchanged.

| Method | Path | Notes |
|---|---|---|
| GET | `/healthz` | `{"status": "ok"}`. No DB: this is the ALB health check, so a DB blip can't take every task out at once. |
| GET | `/readyz` | `SELECT 1`; 200 if the DB answers, 503 if not. |
| GET | `/version` | `{"service", "env", "branch", "sha"}`. Branch and SHA are baked into the image at build time. |
| POST | `/items` | Body `{"name", "description"?}` -> 201 |
| GET | `/items`, `/items/{id}` | |
| PUT | `/items/{id}` | Replaces the whole item. |
| DELETE | `/items/{id}` | 204 |
| GET | `/docs` | OpenAPI UI |

An item is `{id, name, description, created_at}`.

## Commands

One image, several commands: `python -m app <command>` (the image's entrypoint is
`python -m app`, default command `serve`).

| Command | What it does |
|---|---|
| `serve` | Runs the API (uvicorn) on `0.0.0.0:$PORT`. |
| `migrate` | `alembic upgrade head` while holding a Postgres advisory lock, so two tasks can't migrate at once. Fails with a clear message if the DB is at a migration this code doesn't have (see below). |
| `seed` | Inserts a dozen demo items (`service-b Alpha`, `service-b Bravo`, ...) only if `items` is empty. |
| `copy-db` | Initializes a preview DB (`DB_*`) from main's DB (`SOURCE_DB_*`): `pg_dump \| pg_restore` of schema, data and `alembic_version`. Only when the preview DB has no tables yet; otherwise it logs "already initialized, keeping existing data" and exits 0. Refuses any target whose name doesn't start with `<source DB name>__`, so it can never write to main's DB. |

How ECS runs them (each in its own container, in order):

- `main` env: `migrate` -> `seed` -> `serve`, against Aurora with IAM auth.
- Preview env: `copy-db` (main's DB via the read-only `<db>_reader` user -> the preview
  DB, both IAM) -> `migrate` (the branch's migrations on top) -> `serve`. No seed: the data
  comes from main.

Demo data is a command, not a migration: migrations are for schema only.

**Preview data survives pushes.** `copy-db` only copies into an empty preview DB, i.e. when
the env is first created. Every later push keeps the data and just runs the branch's new
migrations on top. `pg_dump` reads in one transaction, so the copy is a consistent snapshot
even while main is being written; the restore is a single transaction too (all or nothing).

**Stale branches fail loudly.** If main merged a migration after your branch was created,
the preview DB (copied from main) is at a revision your branch doesn't know, and `migrate`
fails with: *"this environment's database is at migration <rev>, which this code doesn't
have. If main has moved on, merge or rebase main into your branch. If this preview's
database got ahead of the branch (e.g. a rewritten migration), delete and re-push the
branch to recreate it."* Previewing a stale combination would test something that can never
reach production.

Preview DB users default to `statement_timeout = 30s` (so one preview can't hog the shared
cluster); `migrate` and `copy-db` turn it off for their own sessions.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `SERVICE_NAME` | `service-b` | Shown in `/version` and used in demo item names. |
| `ENV_NAME` | `local` | `main` or the preview group name. |
| `PATH_PREFIX` | `""` | E.g. `/a`. Empty or a leading-slash path with no trailing slash. |
| `PORT` | `8000` | |
| `GIT_SHA`, `GIT_BRANCH` | `unknown` | Set from Docker build args. |
| `DB_HOST`, `DB_PORT`, `DB_NAME`, `DB_USER` | port `5432` | The service's own DB. |
| `DB_AUTH` | `password` | `iam` (Aurora: main and previews) or `password` (local runs, tests). |
| `DB_PASSWORD` | | Password mode only. |
| `DB_SSLMODE` | `prefer` | IAM mode requires `require` or stricter. |
| `DB_POOL_SIZE`, `DB_MAX_OVERFLOW` | `5`, `5` | SQLAlchemy pool. Previews use `2`, `3`: they share main's cluster, and connections run out first. |
| `AWS_REGION` | | IAM mode only (signs the auth token). |
| `SOURCE_DB_*` | | Same fields for `copy-db`'s source (`HOST`, `PORT`, `NAME`, `USER`, `AUTH`, `PASSWORD`, `SSLMODE`). |

**IAM auth:** the SQLAlchemy pool (`DB_POOL_SIZE` + `DB_MAX_OVERFLOW`, `pool_pre_ping=True`)
generates a fresh token (`generate_db_auth_token`, signed locally, no API call) each time
it opens a new connection. Open connections keep working after the token's 15 minutes
expire. All auth-mode logic is in `app/db.py`.

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker. Supported: macOS, Linux, and Windows
via WSL2.

```sh
uv sync
uv run pytest            # starts throwaway Postgres 17 containers (testcontainers)
uv run ruff check && uv run ruff format --check
uv run alembic heads     # must print exactly one head
```

The `copy-db` tests build the app image and run `copy-db` inside it, because `pg_dump`
must be version 17 and the image is what guarantees that. The first run takes a minute or
so; after that, Docker's layer cache makes it quick.

### Run locally against a throwaway Postgres

```sh
docker run -d --rm --name service-b-db -p 5432:5432 \
  -e POSTGRES_USER=service_b -e POSTGRES_PASSWORD=dev -e POSTGRES_DB=service_b postgres:17

export DB_HOST=localhost DB_NAME=service_b DB_USER=service_b DB_PASSWORD=dev DB_SSLMODE=disable
uv run python -m app migrate
uv run python -m app seed
uv run python -m app serve   # http://localhost:8000/items, http://localhost:8000/docs

docker stop service-b-db     # removes it too (--rm)
```

### Adding a migration

```sh
uv run alembic revision --autogenerate -m "add something"   # needs the DB_* vars above
```

Keep migrations backward compatible (expand, then contract): during a rolling deploy the
old code runs against the new schema for a while.

## CI (`.github/workflows/ci.yml`)

- Every push: ruff, pytest, exactly one Alembic head, Docker build.
- Push to `main` or `preview/**`, after those pass: assume an AWS role via OIDC (no stored
  AWS keys), build a `linux/arm64` image on GitHub's ARM runner, push it to ECR as
  `<service>:<full commit SHA>`, then send a `repository_dispatch` (`service-changed`) to
  the infra repo.
- Deleting a `preview/**` branch: `repository_dispatch` only.

Repository settings: variables `AWS_REGION`, `AWS_ROLE_ARN`, `INFRA_REPO` (`owner/name`);
secret `INFRA_DISPATCH_TOKEN` (a token allowed to dispatch to the infra repo).
