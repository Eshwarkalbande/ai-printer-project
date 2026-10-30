"""
MongoDB persistence layer for the AI-Based Printer Monitoring System.

This module exclusively owns *identity* and *audit* data:

    users         - login accounts (username, email, hashed_password, role, ...)
    activity_log  - plain-language audit trail of operator actions

Printer telemetry (printers, print_jobs, telemetry_history, alerts) deliberately
stays in SQLite - see database.py - and is never touched from here.

Design rule: every public helper must survive a MongoDB outage. When the server
cannot be reached the module logs a clear message and returns safe defaults, so
the real-time printer dashboard keeps working even if MongoDB is down.
"""

import os
import time
from datetime import datetime, timezone

from werkzeug.security import check_password_hash, generate_password_hash

try:
    from bson import ObjectId
    from pymongo import ASCENDING, DESCENDING, MongoClient
    from pymongo.errors import DuplicateKeyError
    PYMONGO_AVAILABLE = True
except ImportError:  # driver not installed -> app still runs, MongoDB disabled
    PYMONGO_AVAILABLE = False
    ObjectId = ASCENDING = DESCENDING = MongoClient = None

    class DuplicateKeyError(Exception):
        """Placeholder used only when the pymongo driver is missing."""

# ---------------------------------------------------------------------------
# Configuration (environment variable with a safe local fallback)
# ---------------------------------------------------------------------------

DEFAULT_MONGO_URI = 'mongodb://localhost:27018/smartprint'
MONGO_URI = os.environ.get('MONGO_URI', DEFAULT_MONGO_URI)
MONGO_DB_NAME = os.environ.get('MONGO_DB_NAME', 'smartprint')
MONGO_TIMEOUT_MS = int(os.environ.get('MONGO_TIMEOUT_MS', '2500'))
MONGO_RETRY_SECONDS = int(os.environ.get('MONGO_RETRY_SECONDS', '30'))

USERS_COLLECTION = 'users'
ACTIVITY_COLLECTION = 'activity_log'

DEFAULT_ADMIN_USERNAME = 'admin'
DEFAULT_ADMIN_EMAIL = 'admin@printer.local'
DEFAULT_ADMIN_PASSWORD = 'admin123'
DEFAULT_ADMIN_ROLE = 'admin'

# ---------------------------------------------------------------------------
# Module state
# ---------------------------------------------------------------------------

_client = None
_db = None
_available = False
_last_error = None
_last_attempt = 0.0


def _warn(message):
    print(f"[MongoDB] {message}")


# ---------------------------------------------------------------------------
# Connection handling
# ---------------------------------------------------------------------------

def _ensure_collection(db, name):
    """Create a collection on first run so `db.users` / `db.activity_log` exist."""
    if name not in db.list_collection_names():
        db.create_collection(name)
        _warn(f"created collection '{name}'")


def init_mongo():
    """
    Connect to MongoDB and prepare the `users` and `activity_log` collections.

    Never raises: returns (ok, message). On failure the module stays in a
    disabled state and the rest of the application keeps running.
    """
    global _client, _db, _available, _last_error, _last_attempt

    _last_attempt = time.time()

    if not PYMONGO_AVAILABLE:
        _client, _db, _available = None, None, False
        _last_error = 'the pymongo driver is not installed (pip install pymongo)'
        _warn(f"ERROR: {_last_error}")
        _warn('Continuing without MongoDB. Printer telemetry (SQLite) is unaffected, '
              'but login/registration and activity logging are degraded.')
        return False, _last_error

    try:
        client = MongoClient(
            MONGO_URI,
            serverSelectionTimeoutMS=MONGO_TIMEOUT_MS,
            connectTimeoutMS=MONGO_TIMEOUT_MS,
            socketTimeoutMS=max(MONGO_TIMEOUT_MS, 5000),
            appname='SmartPrint-AI',
        )
        client.admin.command('ping')

        db = client[MONGO_DB_NAME]
        _ensure_collection(db, USERS_COLLECTION)
        _ensure_collection(db, ACTIVITY_COLLECTION)

        db[USERS_COLLECTION].create_index(
            [('username', ASCENDING)], unique=True, name='username_unique')
        db[USERS_COLLECTION].create_index(
            [('email', ASCENDING)], unique=True, name='email_unique')
        db[ACTIVITY_COLLECTION].create_index(
            [('timestamp', DESCENDING)], name='timestamp_desc')
        db[ACTIVITY_COLLECTION].create_index(
            [('printer_id', ASCENDING)], name='printer_id_idx')

        _client, _db, _available, _last_error = client, db, True, None
        _warn(f"connected to {MONGO_URI} (database '{MONGO_DB_NAME}')")
        _seed_default_admin()
        return True, f'connected to {MONGO_URI}'
    except Exception as exc:  # noqa: BLE001 - startup must never crash on this
        _client, _db, _available, _last_error = None, None, False, str(exc)
        _warn('ERROR: could not connect to MongoDB.')
        _warn(f"ERROR: URI = {MONGO_URI}")
        _warn(f"ERROR: {type(exc).__name__}: {exc}")
        _warn('Continuing without MongoDB. Printer telemetry (SQLite) is unaffected, '
              'but login/registration and activity logging are degraded.')
        return False, str(exc)


def _reconnect_if_needed():
    """Throttled lazy reconnect so the app self-heals once MongoDB comes back."""
    if _db is not None:
        return True
    if time.time() - _last_attempt < MONGO_RETRY_SECONDS:
        return False
    ok, _msg = init_mongo()
    return ok


def is_available():
    """True when MongoDB is currently reachable (cheap, non-blocking check)."""
    return _available


def get_status():
    """Diagnostic snapshot used by the status endpoint / startup banner."""
    status = {
        'available': _available,
        'uri': MONGO_URI,
        'database': MONGO_DB_NAME,
        'last_error': _last_error,
        'users': 0,
        'activities': 0,
    }
    if _db is not None:
        try:
            status['users'] = _db[USERS_COLLECTION].count_documents({})
            status['activities'] = _db[ACTIVITY_COLLECTION].count_documents({})
        except Exception as exc:  # noqa: BLE001
            status['last_error'] = str(exc)
    return status


def _collection(name):
    """Collection handle, or None when MongoDB is unavailable."""
    if _db is None:
        return None
    return _db[name]


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _utcnow():
    return datetime.now(timezone.utc)


def _iso(value):
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _public_user(doc):
    """JSON-safe user representation (safe to keep inside the Flask session)."""
    if not doc:
        return None
    uid = doc.get('user_id') or str(doc.get('_id'))
    return {
        'id': uid,
        'user_id': uid,
        'username': doc.get('username'),
        'email': doc.get('email'),
        'role': doc.get('role', 'operator'),
        'created_at': _iso(doc.get('created_at')),
    }


# ---------------------------------------------------------------------------
# users collection
# ---------------------------------------------------------------------------

def _seed_default_admin():
    """Insert the default admin account when the users collection is empty."""
    users = _collection(USERS_COLLECTION)
    if users is None:
        return False
    try:
        if users.count_documents({}) > 0:
            return False
        oid = ObjectId()
        users.insert_one({
            '_id': oid,
            'user_id': str(oid),
            'username': DEFAULT_ADMIN_USERNAME,
            'email': DEFAULT_ADMIN_EMAIL,
            'hashed_password': generate_password_hash(DEFAULT_ADMIN_PASSWORD),
            'role': DEFAULT_ADMIN_ROLE,
            'created_at': _utcnow(),
        })
        _warn(f"seeded default admin account '{DEFAULT_ADMIN_USERNAME}'")
        return True
    except Exception as exc:  # noqa: BLE001
        _warn(f"WARNING: could not seed the default admin account: {exc}")
        return False


def create_user(username, email, password, role='operator'):
    """
    Create a user account. Returns (ok, message).

    Passwords are always stored as a Werkzeug hash - never in plaintext.
    """
    username = (username or '').strip()
    email = (email or '').strip()
    if not username or not email or not password:
        return False, 'All fields are required.'

    if not _reconnect_if_needed():
        return False, f'Authentication database unavailable ({MONGO_URI}).'

    users = _collection(USERS_COLLECTION)
    try:
        if users.find_one({'$or': [{'username': username}, {'email': email.lower()}]}):
            return False, 'That username or email is already registered.'

        oid = ObjectId()
        users.insert_one({
            '_id': oid,
            'user_id': str(oid),
            'username': username,
            'email': email.lower(),
            'hashed_password': generate_password_hash(password),
            'role': role or 'operator',
            'created_at': _utcnow(),
        })
        return True, f"Account '{username}' created."
    except DuplicateKeyError:
        return False, 'That username or email is already registered.'
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: could not create user '{username}': {exc}")
        return False, f'Could not create account: {exc}'


def authenticate_user(username_or_email, password):
    """
    Verify credentials against the `users` collection.

    Accepts either a username or an email address in the first argument.
    Returns a JSON-safe user dict on success, otherwise None.
    """
    if not username_or_email or not password:
        return None

    if not _reconnect_if_needed():
        return None

    users = _collection(USERS_COLLECTION)
    identifier = username_or_email.strip()
    try:
        doc = users.find_one({'$or': [
            {'username': identifier},
            {'email': identifier.lower()},
        ]})
        if doc and check_password_hash(doc.get('hashed_password', ''), password):
            return _public_user(doc)
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: authentication lookup failed: {exc}")
    return None


def get_user(username_or_email):
    """Fetch a single user document (JSON safe) or None."""
    users = _collection(USERS_COLLECTION)
    if users is None or not username_or_email:
        return None
    identifier = username_or_email.strip()
    try:
        return _public_user(users.find_one({'$or': [
            {'username': identifier}, {'email': identifier.lower()}]}))
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: user lookup failed: {exc}")
        return None


def count_users():
    users = _collection(USERS_COLLECTION)
    if users is None:
        return 0
    try:
        return users.count_documents({})
    except Exception:  # noqa: BLE001
        return 0


# ---------------------------------------------------------------------------
# activity_log collection
# ---------------------------------------------------------------------------

def log_activity(username, action, printer_id=None):
    """
    Append one audit document to `activity_log`.

    `printer_id` is stored as null for actions that are not printer specific.
    Never raises - if MongoDB is down this only prints a warning.
    """
    if not action:
        return False

    if not _reconnect_if_needed():
        _warn(f"activity_log unavailable - '{action}' was not recorded")
        return False

    try:
        _collection(ACTIVITY_COLLECTION).insert_one({
            'username': username or 'unknown',
            'action': action,
            'printer_id': printer_id,
            'timestamp': _utcnow(),
        })
        return True
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: could not write activity_log entry '{action}': {exc}")
        return False


def get_recent_activities(limit=50):
    """Most recent activity_log documents (newest first), JSON safe."""
    activities = _collection(ACTIVITY_COLLECTION)
    if activities is None:
        return []
    try:
        docs = activities.find().sort('timestamp', DESCENDING).limit(int(limit))
        return [{
            'id': str(doc['_id']),
            'username': doc.get('username'),
            'action': doc.get('action'),
            'printer_id': doc.get('printer_id'),
            'timestamp': _iso(doc.get('timestamp')),
        } for doc in docs]
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: could not read activity_log: {exc}")
        return []


def reset_collections():
    """Drop and recreate both collections (test helper)."""
    if not _reconnect_if_needed():
        return False
    try:
        _db[USERS_COLLECTION].drop()
        _db[ACTIVITY_COLLECTION].drop()
        return init_mongo()[0]
    except Exception as exc:  # noqa: BLE001
        _warn(f"ERROR: could not reset collections: {exc}")
        return False
