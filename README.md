# discussions

Peer discovery and signaling service for [CList](https://github.com/Downes/CList) P2P chat.

CList users can start a named discussion and advertise it here so others can find and join it. Actual chat messages travel directly between browsers over WebRTC (via [PeerJS](https://peerjs.com/)) — this server is only a bulletin board for peer IDs. It holds no message content and stores no history.

---

## How it works

1. A user starts a discussion in CList. The client POSTs `{ name, peerId }` to `/api/discussions`, advertising their PeerJS peer ID under a human-readable name.
2. Other users GET `/api/discussions` to see the list of active discussions and their peer IDs.
3. The joining user connects directly to the host's browser via PeerJS/WebRTC. This server is no longer involved.
4. The host client re-POSTs every 60 seconds as a heartbeat. Discussions with no heartbeat for 5 minutes are automatically pruned from the list.
5. When the host ends the discussion, the client sends DELETE to remove it immediately.

---

## Auth

All endpoints require a valid Bearer token issued by [kvstore](https://github.com/Downes/kvstore). Each request is verified by calling `GET /auth/verify` on the configured kvstore instance. Verified tokens are cached in memory for 5 minutes to keep latency low.

---

## API

All endpoints except `/health` require an `Authorization: Bearer <token>` header.

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/discussions` | List all active (non-expired) discussions |
| `POST` | `/api/discussions` | Advertise a new discussion, or heartbeat an existing one |
| `DELETE` | `/api/discussions` | End a discussion immediately |
| `GET` | `/health` | Health check — returns `{"status":"ok"}` |

### POST /api/discussions

```json
{ "name": "My Discussion", "peerId": "abc123-..." }
```

If a discussion with the same `name` already exists, its timestamp is refreshed (heartbeat). Otherwise a new entry is created. Returns `201`.

### DELETE /api/discussions

```json
{ "name": "My Discussion" }
```

Returns `200` on success, `404` if the discussion is not found.

---

## Data

State is stored in a flat JSON file at `$DATA_DIR/discussions.json`. There is no database. The file is rewritten on every write operation.

```json
[
  { "name": "My Discussion", "peerId": "abc123-...", "timestamp": 1234567890 }
]
```

Entries with a `timestamp` older than 300 seconds are pruned on every read and write.

---

## Environment variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `KVSTORE_URL` | `http://kvstore:5000` | kvstore base URL for token verification |
| `DATA_DIR` | `/data` | Directory for `discussions.json` |

---

## Deployment

Runs in Docker, proxied by Caddy at `discussions.mooc.ca`.

```bash
# Build and start
cd /srv/apps/discussions
docker compose up -d --build

# View logs
docker logs discussions

# Health check
curl https://discussions.mooc.ca/health
```

The `data/` directory is bind-mounted from the host so state survives container rebuilds. Because discussions are ephemeral (5-minute expiry), data loss on restart is harmless.

---

## Related projects

- [CList](https://github.com/Downes/CList) — the client-side app that uses this server (`js/dynamicp2p.js`)
- [kvstore](https://github.com/Downes/kvstore) — credential store that handles authentication
