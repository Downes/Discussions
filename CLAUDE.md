# discussions

Peer discovery / signaling service for CList P2P chat.

## Overview

Stores ephemeral peer IDs so CList users can find and join each other's
PeerJS discussions. Actual chat is peer-to-peer (WebRTC); this server
only acts as a bulletin board.

## Stack
- Python 3.11 / Flask
- Gunicorn (2 workers)
- Flat JSON file for state (`/data/discussions.json`)
- Auth is local JWT/JWKS verification ("Pattern B"), see Auth below

## Container
| Name         | Port  | Network |
|--------------|-------|---------|
| discussions  | 8082  | web     |

## Domain
`discussions.mooc.ca` — LIVE

## Auth
All endpoints (except `/api/discussions/public` and `/health`) require a
kvstore Bearer token.
- Token verification is local JWT/JWKS ("Pattern B") via the shared
  `clist-kvstore-auth` package (`clist_kvstore_auth.sync`), pinned by commit
  SHA in `requirements.txt` (github.com/Downes/clist-kvstore-auth),
  `_verify_token()` in `app.py`. A JWT's own `iss` claim names the instance
  that signed it; we fetch *that* instance's `/.well-known/jwks.json` and
  verify the signature — open to any issuer, no allowlist (anyone can run
  their own kvstore and interoperate).
- Opaque (pre-migration) tokens carry no `iss` and can't be verified
  locally, so they still fall back to `GET {KVSTORE_URL}/auth/verify`,
  cached in memory for `CACHE_TTL` (5 minutes) keyed by raw token. Real JWTs
  are **not** cached here — local verification is already fast (PyJWKClient
  caches each issuer's JWKS in-process), and caching a JWT's result would
  let it keep working past its own `exp` claim for up to the cache TTL.
- `token_required` injects the verified `Identity` (not a bare username) as
  the decorated view's first argument.
- Owner enforcement: a discussion's `owner` field stores the qualified
  identity (`did:web:<kvstore-host>:users:<username>`), not a bare
  username — a bare username is not a complete identity once any kvstore
  instance can mint one. Ownership checks (the per-user discussion cap, and
  "only the owner may delete it") compare via `clist_kvstore_auth.sync.owns()`,
  which also accepts a legacy bare-username match for pre-migration rows,
  as long as the token's issuer is this app's own `KVSTORE_URL`.
- `/admin/settings` is additionally gated by a locally-reimplemented
  `is_admin(identity)` in `app.py`, not `clist_kvstore_auth.sync.is_admin()`
  directly, because `KVSTORE_URL` is DB-overridable here (not just a static
  env var) — see Admin settings below.
- **`KVSTORE_URL` must be the kvstore instance's public HTTPS URL** (e.g.
  `https://kvstore.mooc.ca`), matching its signed tokens' `iss` claim — NOT
  the internal Docker-network address (`http://kvstore:5000`) this app used
  before the Pattern B migration. The internal address still works fine for
  the opaque-token fallback call itself, but it breaks both the admin-issuer
  check and the legacy-owner fallback, since a real JWT's `iss` is always
  the public URL, never the internal one. (Discovered 2026-10-23 when
  migrating this app — `collab`/`proxyp` default to the same internal
  address and will need the same fix if/when they're migrated.)

## Environment
| Variable        | Default                  | Purpose                          |
|-----------------|---------------------------|-----------------------------------|
| KVSTORE_URL     | https://kvstore.mooc.ca   | This app's own kvstore instance — opaque-token fallback target and sole trusted admin issuer (overridable at runtime, see Admin settings). Must be the public HTTPS URL, not an internal Docker address. |
| DATA_DIR        | /data                     | directory for discussions.json and settings.db |
| ADMIN_USERNAMES | (none)                    | Comma-separated usernames allowed to use `/admin/settings` |

## API
| Method  | Path               | Auth          | Description                              |
|---------|--------------------|---------------|-------------------------------------------|
| GET     | /api/discussions   | Bearer        | List active (non-expired) discussions    |
| POST    | /api/discussions   | Bearer        | Advertise or heartbeat a discussion      |
| DELETE  | /api/discussions   | Bearer        | End a discussion                         |
| GET     | /health            | none          | Health check                             |
| GET/PUT | /admin/settings    | Bearer, admin | Generic admin-editable settings pattern (see below) |

Discussions expire after 5 minutes without a heartbeat.
The CList client re-POSTs every 60 seconds to keep a discussion alive.

## Admin settings

Generic admin-editable runtime settings — imported from the shared
`clist-server-admin` package (`clist_server_admin.sqlite`), pinned by commit
SHA in `requirements.txt` (github.com/Downes/clist-server-admin). No local
`admin_settings.py` copy anymore. `ADMIN_USERNAMES` (comma-separated, in
`docker-compose.yml`) gates `/admin/settings` (Bearer token + username in
that set, else 403).

Currently exposes two fields: `ENABLED` (boolean, default `true`) — kill
switch; when set to `false`, a `before_request` hook returns `503
{"error": "Service temporarily disabled by administrator"}` for every route
except `/admin/settings` and `/health` — and `KVSTORE_URL` (non-sensitive) —
this app's own kvstore instance, used as the opaque-token fallback target
and as the sole trusted issuer for `ADMIN_USERNAMES` admin access (does NOT
gate ordinary token verification, which is local JWT/JWKS and open to any
issuer). This app has no other SQLite database (discussions themselves live
in the flat `discussions.json` file), so settings get their own tiny
`app_settings` table in a separate `settings.db` in `DATA_DIR`, created on
startup via `init_settings_db()`.

A value saved through `PUT /admin/settings` takes effect immediately, no
restart — `get_setting()` is read fresh on every token verification and
overrides the `KVSTORE_URL` env var, which stays the fallback until an admin
explicitly saves an override.

## Data format (discussions.json)
```json
[
  {"name": "My Discussion", "peerId": "uuid-...", "timestamp": 1234567890}
]
```

## CList client
`dynamicp2p.js` points to `https://discussions.mooc.ca/api/discussions` — already wired up.
