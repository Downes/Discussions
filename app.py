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
import sqlite3
import requests
from functools import wraps
from flask import Flask, request, jsonify, g

from admin_settings import SettingField, init_settings_table, get_setting, register_admin_settings_routes

app = Flask(__name__)

DATA_DIR = os.environ.get('DATA_DIR', '/data')
SETTINGS_DB_PATH = os.path.join(DATA_DIR, 'settings.db')
DISCUSSIONS_FILE = os.path.join(DATA_DIR, 'discussions.json')
DISCUSSION_EXPIRY = 300   # seconds — discussion expires if no heartbeat received
CACHE_TTL = 300           # seconds — how long a verified token stays trusted
MAX_DISCUSSIONS_TOTAL = 100   # hard cap on total active discussions
MAX_DISCUSSIONS_PER_USER = 5  # per-user cap to prevent flooding

SETTINGS_SCHEMA = [
    SettingField(
        key="ENABLED", label="Enabled", type="boolean", sensitive=False, validate=None,
        help="When off, this app's functional endpoints return 503. /admin/settings "
             "and /health stay reachable so you can turn it back on.",
    ),
    SettingField(
        key="KVSTORE_URL", label="KVStore URL", type="url", sensitive=False, validate=None,
        help="kvstore instance used to verify every request's Bearer token "
             "(GET {KVSTORE_URL}/auth/verify). Only change this if discussions "
             "should authenticate against a different kvstore server.",
    ),
]
SETTINGS_DEFAULTS = {
    "ENABLED": "true",
    "KVSTORE_URL": os.environ.get('KVSTORE_URL', 'http://kvstore:5000'),
}


def get_settings_db():
    if 'settings_db' not in g:
        g.settings_db = sqlite3.connect(SETTINGS_DB_PATH)
        g.settings_db.row_factory = sqlite3.Row
    return g.settings_db


@app.teardown_appcontext
def close_settings_db(e=None):
    db = g.pop('settings_db', None)
    if db:
        db.close()


def init_settings_db():
    os.makedirs(DATA_DIR, exist_ok=True)
    db = sqlite3.connect(SETTINGS_DB_PATH)
    init_settings_table(db)
    db.close()


init_settings_db()


def _kvstore_url():
    return get_setting(get_settings_db(), "KVSTORE_URL", default=SETTINGS_DEFAULTS["KVSTORE_URL"])


@app.before_request
def _enabled_gate():
    if request.path in ('/admin/settings', '/health'):
        return None
    if get_setting(get_settings_db(), "ENABLED", default="true") == "false":
        return jsonify({"error": "Service temporarily disabled by administrator"}), 503


# ── Auth ─────────────────────────────────────────────────────────────────────

_token_username_cache: dict[str, tuple[float, str]] = {}  # token → (expiry, username)


def _verify_token(token: str) -> str | None:
    """Return the username if the token is valid, None otherwise.
    Valid results are cached for CACHE_TTL seconds."""
    now = time.time()
    if token in _token_username_cache:
        expiry, username = _token_username_cache[token]
        if expiry > now:
            return username
    try:
        resp = requests.get(
            f'{_kvstore_url()}/auth/verify',
            headers={'Authorization': f'Bearer {token}'},
            timeout=5,
        )
    except requests.RequestException:
        return None
    if resp.status_code == 200:
        username = resp.json().get('username') or ''
        if username:
            _token_username_cache[token] = (now + CACHE_TTL, username)
            return username
    _token_username_cache.pop(token, None)
    return None


def token_required(f):
    """Decorator: reject requests without a valid kvstore Bearer token.
    Injects the verified username as the first positional argument."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        auth = request.headers.get('Authorization', '')
        parts = auth.split()
        if len(parts) != 2 or parts[0].lower() != 'bearer':
            return jsonify({'error': 'Authorization header required'}), 401
        username = _verify_token(parts[1])
        if not username:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(username, *args, **kwargs)
    return wrapper


def verify_token_for_admin():
    """(username, None) on success, (None, (body, status)) on failure —
    the shape register_admin_settings_routes expects."""
    auth = request.headers.get('Authorization', '')
    parts = auth.split()
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return None, ({'error': 'Authorization header required'}, 401)
    username = _verify_token(parts[1])
    if not username:
        return None, ({'error': 'Unauthorized'}, 401)
    return username, None


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
def manage_discussions(username):
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

        is_public = bool(data.get('public', False))
        discussions = _prune(_load())
        # Check if this is a heartbeat for an existing discussion.
        for d in discussions:
            if d['name'] == name:
                d['timestamp'] = int(time.time())   # heartbeat — refresh expiry
                _save(discussions)
                return jsonify({'message': 'Discussion advertised successfully'}), 201
        # New discussion — enforce caps before appending.
        if len(discussions) >= MAX_DISCUSSIONS_TOTAL:
            return jsonify({'error': 'Server discussion limit reached'}), 503
        user_count = sum(1 for d in discussions if d.get('owner') == username)
        if user_count >= MAX_DISCUSSIONS_PER_USER:
            return jsonify({'error': 'Per-user discussion limit reached'}), 429
        discussions.append({'name': name, 'peerId': peer_id, 'owner': username,
                             'public': is_public, 'timestamp': int(time.time())})
        _save(discussions)
        return jsonify({'message': 'Discussion advertised successfully'}), 201

    if request.method == 'DELETE':
        # End a discussion — only the owner may delete it.
        data = request.get_json(silent=True) or {}
        name = data.get('name', '').strip()
        if not name:
            return jsonify({'error': 'name is required'}), 400

        discussions = _prune(_load())
        target = next((d for d in discussions if d['name'] == name), None)
        if target is None:
            return jsonify({'error': 'Discussion not found'}), 404
        if target.get('owner') and target['owner'] != username:
            return jsonify({'error': 'Forbidden'}), 403
        updated = [d for d in discussions if d['name'] != name]
        _save(updated)
        return jsonify({'message': 'Discussion ended'}), 200


@app.route('/api/discussions/public', methods=['GET'])
def public_discussions():
    """Return active public discussions — no auth required."""
    discussions = _prune(_load())
    _save(discussions)
    public = [{'name': d['name'], 'peerId': d['peerId']} for d in discussions if d.get('public')]
    return jsonify(public), 200


@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


register_admin_settings_routes(app, SETTINGS_SCHEMA, get_settings_db, verify_token_for_admin, SETTINGS_DEFAULTS)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8082)
