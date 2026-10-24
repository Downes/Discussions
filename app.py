"""
discussions — peer discovery / signaling service for CList P2P chat

Users advertise an active discussion (name + PeerJS peer ID) so others
can find and join it. Actual chat messages travel peer-to-peer via WebRTC;
this server only stores the ephemeral peer IDs.

All endpoints require a valid kvstore Bearer token, verified locally via
JWT/JWKS ("Pattern B") against the issuer named in the token's own `iss`
claim — open to any kvstore instance, no allowlist. Pre-migration opaque
tokens fall back to KVSTORE_URL/auth/verify.
"""

import os
import json
import time
import sqlite3
from functools import wraps
from flask import Flask, request, jsonify, g

from clist_server_admin.sqlite import SettingField, init_settings_table, get_setting, register_admin_settings_routes
from clist_kvstore_auth.sync import verify_token as _kv_verify_token, owns as _kv_owns

app = Flask(__name__)

ADMIN_USERNAMES = {u.strip() for u in os.environ.get("ADMIN_USERNAMES", "").split(",") if u.strip()}

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
        help="This app's own kvstore instance — used as the fallback target "
             "for pre-migration opaque tokens (GET {KVSTORE_URL}/auth/verify) "
             "and as the sole trusted issuer for ADMIN_USERNAMES admin "
             "access. Does NOT gate ordinary token verification, which is "
             "local JWT/JWKS and open to any issuer's JWT.",
    ),
]
SETTINGS_DEFAULTS = {
    "ENABLED": "true",
    "KVSTORE_URL": os.environ.get('KVSTORE_URL', 'https://kvstore.mooc.ca'),
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

# Only opaque/legacy tokens are cached — real JWTs are verified fresh every
# time, since local JWKS verification is already fast (PyJWKClient caches
# the JWKS itself per-issuer) and caching the result here would let an
# expired JWT keep working past its own `exp` claim for up to CACHE_TTL.
_token_identity_cache: dict[str, tuple[float, object]] = {}  # token → (expiry, Identity)


def _is_jwt(token: str) -> bool:
    return token.count('.') == 2


def _verify_token(auth: str):
    """Return (Identity, None) on success, (None, (body, status)) on failure."""
    token = auth[7:].strip() if auth.lower().startswith('bearer ') else ''
    if not token:
        return None, ({'error': 'Authorization header required'}, 401)
    if _is_jwt(token):
        return _kv_verify_token(auth, _kvstore_url())
    now = time.time()
    cached = _token_identity_cache.get(token)
    if cached:
        expiry, identity = cached
        if expiry > now:
            return identity, None
    identity, err = _kv_verify_token(auth, _kvstore_url())
    if err:
        _token_identity_cache.pop(token, None)
        return None, err
    _token_identity_cache[token] = (now + CACHE_TTL, identity)
    return identity, None


def token_required(f):
    """Decorator: reject requests without a valid kvstore Bearer token.
    Injects the verified Identity as the first positional argument."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        identity, err = _verify_token(request.headers.get('Authorization', ''))
        if err:
            body, status = err
            return jsonify(body), status
        return f(identity, *args, **kwargs)
    return wrapper


def is_admin(identity) -> bool:
    # Reimplemented locally (not clist_kvstore_auth.sync.is_admin) because
    # the trusted admin issuer is this app's own KVSTORE_URL setting, which
    # is DB-overridable via /admin/settings, not just a static env var.
    return bool(identity) and identity.username in ADMIN_USERNAMES and identity.iss == _kvstore_url()


def verify_token_for_admin():
    """(username, None) on success, (None, (body, status)) on failure —
    the shape register_admin_settings_routes expects."""
    identity, err = _verify_token(request.headers.get('Authorization', ''))
    if err:
        return None, err
    if not is_admin(identity):
        return None, ({'error': 'Forbidden'}, 403)
    return identity.username, None


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
def manage_discussions(identity):
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
        kvstore_url = _kvstore_url()
        user_count = sum(1 for d in discussions if _kv_owns(d.get('owner', ''), identity, kvstore_url))
        if user_count >= MAX_DISCUSSIONS_PER_USER:
            return jsonify({'error': 'Per-user discussion limit reached'}), 429
        discussions.append({'name': name, 'peerId': peer_id, 'owner': identity.qualified_id,
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
        if target.get('owner') and not _kv_owns(target['owner'], identity, _kvstore_url()):
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
