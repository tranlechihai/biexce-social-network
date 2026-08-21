# Biexce Social

A Python 3 / FastAPI social backend with PostgreSQL/SQLite persistence, a responsive Jinja2 web demo, transactional seed data, and comprehensive tests.

## Quick Start

### 1. Environment and Dependencies

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[test]"
```

For a reproducible pinned environment instead of live resolution:

```bash
pip install -r requirements.lock -r requirements-dev.lock
pip install --no-deps -e .
```

### 2. Configure

Set a signing secret and a required demo password (never commit these):

```bash
export TING_JWT_SECRET="change-me-to-a-random-string"
export TING_DEMO_PASSWORD="your-chosen-password"  # required, no default
```

Optional knobs (see `docs/RUNBOOK.md` §6 for the full list): upload quota per
user `TING_UPLOAD_QUOTA_MB` (default 512 MB) and fleet-wide
`TING_TOTAL_UPLOAD_QUOTA_MB` (default 5120 MB), JWT expiry
`TING_JWT_EXPIRE_MINUTES`, cookie `TING_COOKIE_SECURE=true` when behind HTTPS.

### 3. Initialize Schema (Create-Only)

Creates MVP tables without dropping or truncating existing data:

```bash
python -m ting_ting
```

### 4. Seed Representative Demo Data (Fresh Database Only)

Requires a fresh, empty, schema-compatible database:

```bash
python -m ting_ting seed
```

**Demo accounts** (password from `TING_DEMO_PASSWORD` — must be set; no default):

| Username | Friends | Notes |
|----------|---------|-------|
| `alice`  | bob     | Has FRIENDS and ONLY_ME posts |
| `bob`    | alice   | Liked and commented on Alice's post |
| `carol`  | —       | Isolated user with a FRIENDS post |

Seed preflight guarantees:
- Supports SQLite for local development and PostgreSQL for deployment.
- Refuses incompatible or pre-populated targets **before any write**.
- Writes all data in a single transaction — any failure rolls back completely.

### 5. Start the Server

```bash
./.venv/bin/python -m uvicorn ting_ting.main:app --reload
```

Open http://localhost:8000/web/feed in a browser (web demo lives under `/web`).

To expose the development server on the internal network:

```bash
export TING_JWT_SECRET="$(openssl rand -hex 32)"
./.venv/bin/python -m uvicorn ting_ting.main:app --host 0.0.0.0 --port 8080
```

Open `http://10.100.0.46:8080/web/login`. Use Nginx + systemd rather than
this foreground command for a persistent server.

Without sudo, install the included user service (the repository `.env` must
exist and is ignored by Git):

```bash
systemctl --user link "$PWD/deploy/systemd/biexce-social-user.service"
systemctl --user enable --now biexce-social-user.service
systemctl --user status biexce-social-user.service
```

Useful operations:

```bash
systemctl --user restart biexce-social-user.service
journalctl --user -u biexce-social-user.service -n 100 --no-pager
systemctl --user disable --now biexce-social-user.service
```

The service cannot survive a full logout/reboot while `loginctl show-user
chihai -p Linger` reports `no`; enabling linger requires an administrator.

### PostgreSQL and Alembic

Set a PostgreSQL URL without committing credentials, then apply the schema:

```bash
export TING_DATABASE_URL='postgresql+psycopg://biexce_social:PASSWORD@127.0.0.1:5432/biexce_social'
./.venv/bin/alembic upgrade head
```

To copy the current SQLite data into a migrated, completely empty PostgreSQL database:

```bash
./.venv/bin/python -m ting_ting.migrate_data --source sqlite:///./ting_ting.db
```

The transfer preserves IDs, verifies every table count, resets PostgreSQL sequences,
and refuses a non-empty target.

### API

- Versioned dual mount: use **`/api/v1/...`** (canonical). The unversioned
  `/api/...` paths still work but return `Deprecation: true` + `Warning: 299`
  headers; they will be removed in the next major version. Interactive docs:
  `/docs`.
- Auth uses short-lived JWTs backed by **server-side sessions**:
  `POST /api/v1/auth/refresh` re-mints the token, `POST /api/v1/auth/logout`
  revokes the current session (token dies even if the cookie survives),
  `POST /api/v1/auth/logout-all` revokes every session, and
  `POST /api/v1/auth/change-password` revokes all other sessions.
- Refresh tokens (T-021): login returns a rotating opaque
  `refresh_token` (and a `ting_ting_refresh` HttpOnly cookie for
  browsers). `POST /api/v1/auth/refresh` accepts it via JSON body or
  cookie and works while the access JWT is expired — the presented token
  is single-use (rotation); re-presenting a rotated token revokes the
  whole session (`401 refresh_replay`). Without a refresh token the
  endpoint falls back to re-minting from a still-valid JWT.
`GET /api/v1/auth/sessions` lists active sessions for the account
   (`current` flags this one); `DELETE /api/v1/auth/sessions/{id}`
   revokes one.
- Account lifecycle (T-023): `GET /api/v1/account/export` returns the
  user's own data as JSON (profile, posts + media, comments, liked/saved/
  reposted ids, following/followers, notifications). `POST
  /api/v1/account/deactivate` (body `{"password": "..."}`) revokes every
  session and hides the account from feeds/search/public profiles — it is
  **reversible** (login still works, unlike a ban); `POST
  /api/v1/account/reactivate` restores it. `POST /api/v1/account/delete`
  (password-confirmed) is **irreversible**: the account and all of its
  content are permanently removed and the username/email stay reserved for
  30 days (deleting accounts keeps their moderation reports as anonymized
  audit evidence for a 30-day retention window). Web: `/web/account` offers
  the export download and the deactivate/reactivate/delete forms (linked
  from your own profile).
- Media delivery is authorized via `/media/{filename}`; uploads are
  validated (magic bytes + blocked-content scan) and quota-limited per user
  and per fleet.
- Observability: `GET /health` (liveness), `GET /ready` (DB check, 503 when
  down), `GET /metrics` (Prometheus text). Every response carries
  `X-Request-ID`, matching the one-line access log (`rid=... path -> status`).

### 6. Run Tests

```bash
# Full regression
./.venv/bin/python -m pytest

# Focused web + seed tests
./.venv/bin/python -m pytest tests/integration -k "web or seed"
```

## Architecture

- `ting_ting/api/` — REST endpoints (JSON)
- `ting_ting/web/routes.py` — Jinja2 web pages (HTML)
- `ting_ting/social.py` — Friendship / block business logic
- `ting_ting/posts.py` — Post and feed business logic
- `ting_ting/interactions.py` — Like and comment business logic
- `ting_ting/api/extensions.py` — Extended profile, follow/activity, saved/repost, and media REST APIs
- `ting_ting/media.py` — Authorized local media storage and delivery
- `ting_ting/uploads.py` — Upload validation (magic bytes, blocked-content scan) + quota
- `ting_ting/moderation.py` — Reports, resolve/dismiss audit, ban/unban, mod content removal
- `ting_ting/sessions.py` — Server-side session registry (revoke/logout-all)
- `ting_ting/notifications.py` — Notification service shared by web and API
- `ting_ting/keyset.py` — Keyset pagination cursors
- `ting_ting/metrics.py` — In-process counters / latency for `/metrics`
- `ting_ting/auth.py` — Auth helpers (cookie + JWT)
- `ting_ting/seed.py` — Guarded transactional seed
- `ting_ting/static/` — CSS / JS assets
- `ting_ting/web/templates/` — Jinja2 HTML templates
