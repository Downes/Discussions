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
| Variable     | Default                | Purpose                          |
|--------------|------------------------|----------------------------------|
| KVSTORE_URL  | http://kvstore:5000    | kvstore auth verify endpoint     |
| DATA_DIR     | /data                  | directory for discussions.json   |

## API
| Method | Path               | Description                              |
|--------|--------------------|------------------------------------------|
| GET    | /api/discussions   | List active (non-expired) discussions    |
| POST   | /api/discussions   | Advertise or heartbeat a discussion      |
| DELETE | /api/discussions   | End a discussion                         |
| GET    | /health            | Health check (no auth)                   |

Discussions expire after 5 minutes without a heartbeat.
The CList client re-POSTs every 60 seconds to keep a discussion alive.

## Data format (discussions.json)
```json
[
  {"name": "My Discussion", "peerId": "uuid-...", "timestamp": 1234567890}
]
```

## Still To Do
- Update `dynamicp2p.js` API_URL to point to discussions.mooc.ca
- Update setup-notes.md
