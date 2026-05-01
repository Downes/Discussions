"""
discussions — peer discovery / signaling service for CList P2P chat

Users advertise an active discussion (name + PeerJS peer ID) so others
can find and join it. Actual chat messages travel peer-to-peer via WebRTC;
this server only stores the ephemeral peer IDs.

All endpoints require a valid kvstore Bearer token (validated via /auth/verify).
"""

import os
import json
import time
import requests
from functools import wraps
from flask import Flask, request, jsonify

app = Flask(__name__)

KVSTORE_URL  = os.environ.get('KVSTORE_URL', 'http://kvstore:5000')
DISCUSSIONS_FILE = os.path.join(os.environ.get('DATA_DIR', '/data'), 'discussions.json')
DISCUSSION_EXPIRY = 300   # seconds — discussion expires if no heartbeat received
CACHE_TTL = 300           # seconds — how long a verified token stays trusted

# In-memory token cache: { token: expiry_timestamp }
_token_cache: dict[str, float] = {}


# ── Auth ─────────────────────────────────────────────────────────────────────

def _verify_token(token: str) -> bool:
    """Return True if the token is valid according to kvstore /auth/verify.
    Valid results are cached for CACHE_TTL seconds to keep latency low."""
    now = time.time()
    if token in _token_cache and _token_cache[token] > now:
        return True
    try:
        resp = requests.get(
            f'{KVSTORE_URL}/auth/verify',
            headers={'Authorization': f'Bearer {token}'},
            timeout=5,
        )
    except requests.RequestException:
        return False
    if resp.status_code == 200:
        _token_cache[token] = now + CACHE_TTL
        return True
    _token_cache.pop(token, None)
    return False


def token_required(f):
    """Decorator: reject requests that don't carry a valid kvstore Bearer token."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return jsonify({'error': 'Authorization header required'}), 401
        if not _verify_token(parts[1]):
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return wrapper


# ── Helpers ───────────────────────────────────────────────────────────────────

def _load() -> list:
    """Load discussions from disk, returning an empty list on any error."""
    try:
        with open(DISCUSSIONS_FILE, 'r') as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _save(discussions: list) -> None:
    os.makedirs(os.path.dirname(DISCUSSIONS_FILE), exist_ok=True)
    with open(DISCUSSIONS_FILE, 'w') as fh:
        json.dump(discussions, fh)


def _prune(discussions: list) -> list:
    """Remove discussions that haven't sent a heartbeat within DISCUSSION_EXPIRY."""
    cutoff = int(time.time()) - DISCUSSION_EXPIRY
    return [d for d in discussions if d.get('timestamp', 0) >= cutoff]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.route('/api/discussions', methods=['GET', 'POST', 'DELETE'])
@token_required
def manage_discussions():
    if request.method == 'GET':
        # Return all active (non-expired) discussions.
        discussions = _prune(_load())
        _save(discussions)
        return jsonify(discussions), 200

    if request.method == 'POST':
        # Advertise or heartbeat a discussion.
        data = request.get_json(silent=True) or {}
        name    = data.get('name', '').strip()
        peer_id = data.get('peerId', '').strip()
        if not name or not peer_id:
            return jsonify({'error': 'name and peerId are required'}), 400

        discussions = _prune(_load())
        for d in discussions:
            if d['name'] == name:
                d['timestamp'] = int(time.time())   # heartbeat — refresh expiry
                break
        else:
            discussions.append({'name': name, 'peerId': peer_id,
                                 'timestamp': int(time.time())})
        _save(discussions)
        return jsonify({'message': 'Discussion advertised successfully'}), 201

    if request.method == 'DELETE':
        # End a discussion.
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400

        discussions = _prune(_load())
        updated = [d for d in discussions if d['name'] != name]
        if len(updated) == len(discussions):
            return jsonify({'error': 'Discussion not found'}), 404
        _save(updated)
        return jsonify({'message': 'Discussion ended'}), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8082)
