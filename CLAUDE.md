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
- Auth delegated to kvstore `/auth/verify` (same pattern as proxyp)

## Container
| Name         | Port  | Network |
|--------------|-------|---------|
| discussions  | 8082  | web     |

## Domain
`discussions.mooc.ca` — LIVE

## Auth
All endpoints require a valid kvstore Bearer token.
Tokens are verified via `KVSTORE_URL/auth/verify` and cached in memory
for 5 minutes. The kvstore container must be reachable on the `web` network.

## Environment
| Variable        | Default                | Purpose                          |
|-----------------|------------------------|-----------------------------------|
| KVSTORE_URL     | http://kvstore:5000    | Default kvstore auth verify endpoint (overridable at runtime, see Admin settings) |
| DATA_DIR        | /data                  | directory for discussions.json and settings.db |
| ADMIN_USERNAMES | (none)                 | Comma-separated usernames allowed to use `/admin/settings` |

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

Generic admin-editable runtime settings — see `/srv/shared/admin_settings.py`
(reference copy; this app's own `admin_settings.py` is a verbatim copy, not
an import). `ADMIN_USERNAMES` (comma-separated, in `docker-compose.yml`) gates
`/admin/settings` (Bearer token + username in that set, else 403).

Currently exposes two fields: `ENABLED` (boolean, default `true`) — kill
switch; when set to `false`, a `before_request` hook returns `503
{"error": "Service temporarily disabled by administrator"}` for every route
except `/admin/settings` and `/health` — and `KVSTORE_URL` (non-sensitive) —
the kvstore instance used to verify every request's Bearer token. This app has no other
SQLite database (discussions themselves live in the flat `discussions.json`
file), so settings get their own tiny `app_settings` table in a separate
`settings.db` in `DATA_DIR`, created on startup via `init_settings_db()`.

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
