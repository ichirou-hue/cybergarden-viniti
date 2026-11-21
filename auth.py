"""Small, stdlib-only cookie-session authentication service."""
import base64
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import time
import uuid

SCHEME = "pbkdf2_sha256"
DEFAULT_ITERATIONS = 210_000
MIN_ITERATIONS = 100_000
MAX_ITERATIONS = 2_000_000
COOKIE_NAME = "viniti_session"
USERNAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{2,31}\Z")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL, username_key TEXT NOT NULL UNIQUE,
 password_hash TEXT NOT NULL, created_at REAL NOT NULL, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
 token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 csrf_token TEXT NOT NULL, created_at REAL NOT NULL, expires_at REAL NOT NULL,
 last_seen_at REAL NOT NULL, client TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS sessions_expiry_idx ON sessions(expires_at);
CREATE TABLE IF NOT EXISTS auth_failures (
 id INTEGER PRIMARY KEY AUTOINCREMENT, username_key TEXT NOT NULL,
 client TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS auth_failures_idx ON auth_failures(username_key, client, created_at);
CREATE TABLE IF NOT EXISTS document_owners (
 area TEXT NOT NULL, document_id TEXT NOT NULL, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 created_at REAL NOT NULL, PRIMARY KEY(area, document_id)
);
"""

class _ManagedConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()

class AuthError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message); self.message = message; self.status = status

def ensure_schema(conn):
    conn.executescript(SCHEMA)
    # Idempotent migration marker.  The current schema is additive, but keep
    # the version in SQLite so future auth migrations are explicit and safe.
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version < 1:
        conn.execute("PRAGMA user_version = 1")

def hash_password(password, iterations=DEFAULT_ITERATIONS):
    iterations = int(iterations)
    if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
        raise ValueError("Недопустимое число итераций PBKDF2.")
    if not isinstance(password, str) or not 8 <= len(password) <= 256:
        raise ValueError("Пароль должен содержать от 8 до 256 символов.")
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"{SCHEME}${iterations}${salt.hex()}${digest.hex()}"

def verify_password(password, encoded):
    try:
        scheme, raw_iterations, salt_hex, digest_hex = encoded.split("$", 3)
        if scheme != SCHEME:
            return False
        iterations = int(raw_iterations)
        if not MIN_ITERATIONS <= iterations <= MAX_ITERATIONS:
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt, iterations)
    return hmac.compare_digest(actual, expected)

class Auth:
    def __init__(self, path, *, iterations=None, session_ttl=None, clock=time.time):
        self.path = str(path)
        self.iterations = int(iterations or os.environ.get("VINITI_AUTH_ITERATIONS", DEFAULT_ITERATIONS))
        self.session_ttl = int(session_ttl or os.environ.get("VINITI_AUTH_SESSION_TTL", 7 * 86400))
        self.clock = clock
        self.cookie_secure = os.environ.get("VINITI_AUTH_COOKIE_SECURE", "0").lower() in {"1", "true", "yes"}

    def connect(self):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, factory=_ManagedConnection)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA journal_mode=WAL")
        ensure_schema(conn)
        return conn

    @staticmethod
    def user(row):
        return {"id": row["id"], "username": row["username"], "created_at": row["created_at"]}

    def register(self, username, password):
        if not isinstance(username, str) or not USERNAME_RE.fullmatch(username):
            raise AuthError("Некорректное имя пользователя.", 422)
        try:
            password_hash = hash_password(password, self.iterations)
        except ValueError as error:
            raise AuthError(str(error), 422) from error
        now = self.clock(); user_id = uuid.uuid4().hex
        try:
            with self.connect() as conn:
                conn.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?, 0)",
                             (user_id, username, username.lower(), password_hash, now))
        except sqlite3.IntegrityError as error:
            raise AuthError("Имя пользователя уже занято.", 409) from error
        return {"id": user_id, "username": username, "created_at": now}

    def login(self, username, password, client="", user_agent="", old_token=None):
        key = str(username or "").lower(); now = self.clock()
        with self.connect() as conn:
            conn.execute("DELETE FROM auth_failures WHERE created_at < ?", (now - 900,))
            count = conn.execute("SELECT COUNT(*) FROM auth_failures WHERE username_key=? AND client=? AND created_at>=?", (key, client, now - 900)).fetchone()[0]
            if count >= 5:
                raise AuthError("Слишком много попыток входа. Повторите позже.", 429)
            row = conn.execute("SELECT * FROM users WHERE username_key=?", (key,)).fetchone()
            valid = row is not None and not row["disabled"] and verify_password(password, row["password_hash"])
            if not valid:
                conn.execute("INSERT INTO auth_failures(username_key,client,created_at) VALUES(?,?,?)", (key, client, now))
                raise AuthError("Неверное имя пользователя или пароль.", 401)
            conn.execute("DELETE FROM auth_failures WHERE username_key=?", (key,))
            if old_token:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash(old_token),))
            token = secrets.token_urlsafe(32); csrf = secrets.token_urlsafe(32)
            conn.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)", (self.token_hash(token), row["id"], csrf, now, now + self.session_ttl, now, client, user_agent[:500]))
            return token, self.user(row), csrf, now + self.session_ttl

    @staticmethod
    def token_hash(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def authenticate(self, token):
        if not token:
            return None
        now = self.clock()
        with self.connect() as conn:
            row = conn.execute("SELECT s.*, u.username, u.created_at AS user_created, u.disabled FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?", (self.token_hash(token),)).fetchone()
            if row is None or row["disabled"] or row["expires_at"] <= now:
                if row is not None:
                    conn.execute("DELETE FROM sessions WHERE token_hash=?", (row["token_hash"],))
                return None
            conn.execute("UPDATE sessions SET last_seen_at=? WHERE token_hash=?", (now, row["token_hash"]))
            return {"token": token, "csrf_token": row["csrf_token"], "expires_at": row["expires_at"], "user": {"id": row["user_id"], "username": row["username"], "created_at": row["user_created"]}}

    def logout(self, token):
        if token:
            with self.connect() as conn:
                conn.execute("DELETE FROM sessions WHERE token_hash=?", (self.token_hash(token),))

    def claim_document(self, area, document_id, user_id):
        """Attach an unowned document to ``user_id`` and report the outcome.

        Content-addressed ingest returns the stored document for a repeated
        upload.  The first uploader keeps ownership: a second user gets
        ``"conflict"`` instead of a document the catalog will hide from them.
        """
        if not document_id or user_id is None:
            return "unowned"
        with self.connect() as conn:
            conn.execute("INSERT INTO document_owners(area, document_id, user_id, created_at) VALUES(?,?,?,?) ON CONFLICT(area, document_id) DO NOTHING", (area, document_id, user_id, self.clock()))
            row = conn.execute("SELECT user_id FROM document_owners WHERE area=? AND document_id=?", (area, document_id)).fetchone()
        if row is None:
            return "unowned"
        return "owned" if row["user_id"] == user_id else "conflict"

    def owns_document(self, area, document_id, user_id):
        with self.connect() as conn:
            row = conn.execute("SELECT user_id FROM document_owners WHERE area=? AND document_id=?", (area, document_id)).fetchone()
        return row is not None and user_id is not None and row["user_id"] == user_id

def cookie_header(token, secure=False, max_age=7 * 86400):
    bits = [f"viniti_session={token}", "Path=/", "HttpOnly", "SameSite=Lax", f"Max-Age={max_age}"]
    if secure:
        bits.append("Secure")
    return "; ".join(bits)

def clear_cookie():
    return "viniti_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
