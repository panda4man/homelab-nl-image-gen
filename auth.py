"""Authentication: local (username/password) + Google OAuth, plus the MCP
API-key boundary.

This module owns everything session-related so `server.py` stays focused on
generation. It exposes:
  - `init_auth(app)` -- wires Flask-Login + Authlib and registers auth routes.
  - `require_auth`   -- decorator accepting a logged-in session OR the shared
                        MCP API key (X-API-Key header).
  - `current_actor()`-- resolves the caller to (user_id, origin) for attribution.
  - small `users` table helpers used by the routes.
"""
import functools
import sqlite3
from datetime import datetime, timezone

from authlib.integrations.flask_client import OAuth
from flask import (
    Blueprint,
    Response,
    g,
    jsonify,
    redirect,
    request,
    url_for,
)
from flask_login import (
    LoginManager,
    UserMixin,
    current_user,
    login_user,
    logout_user,
)
from werkzeug.security import check_password_hash, generate_password_hash

from config import (
    GALLERY_DB_PATH,
    GOOGLE_CLIENT_ID,
    GOOGLE_CLIENT_SECRET,
    MCP_API_KEY,
    OAUTH_REDIRECT_BASE,
)

login_manager = LoginManager()
oauth = OAuth()
auth_bp = Blueprint("auth", __name__)

GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"


# --- DB helpers -------------------------------------------------------------


def _connect():
    conn = sqlite3.connect(GALLERY_DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_user(row):
    return User(row) if row else None


def get_user_by_id(user_id):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def get_user_by_username(username):
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE username = ?", (username,)
        ).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def get_user_by_email(email):
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return _row_to_user(row)
    finally:
        conn.close()


def create_local_user(username, email, password):
    """Create a username/password account. Raises sqlite3.IntegrityError if the
    username or email is already taken (UNIQUE constraint)."""
    conn = _connect()
    try:
        cur = conn.execute(
            """
            INSERT INTO users (username, email, password_hash, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                username,
                email or None,
                generate_password_hash(password),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
        return get_user_by_id(cur.lastrowid)
    finally:
        conn.close()


def upsert_oauth_user(provider, sub, email):
    """Find-or-create an OAuth account keyed by (provider, stable subject id).
    If an account with the same email already exists (e.g. a local signup), link
    it to this provider rather than creating a duplicate."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE oauth_provider = ? AND oauth_sub = ?",
            (provider, sub),
        ).fetchone()
        if row:
            return _row_to_user(row)

        if email:
            existing = conn.execute(
                "SELECT * FROM users WHERE email = ?", (email,)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE users SET oauth_provider = ?, oauth_sub = ? WHERE id = ?",
                    (provider, sub, existing["id"]),
                )
                conn.commit()
                return get_user_by_id(existing["id"])

        cur = conn.execute(
            """
            INSERT INTO users (username, email, oauth_provider, oauth_sub, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (None, email or None, provider, sub, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        return get_user_by_id(cur.lastrowid)
    finally:
        conn.close()


# --- Flask-Login user ------------------------------------------------------


class User(UserMixin):
    def __init__(self, row):
        self.id = row["id"]
        self.username = row["username"]
        self.email = row["email"]
        self.password_hash = row["password_hash"]

    def get_id(self):
        return str(self.id)

    @property
    def display_name(self):
        return self.username or self.email or f"user{self.id}"


@login_manager.user_loader
def _load_user(user_id):
    try:
        return get_user_by_id(int(user_id))
    except (TypeError, ValueError):
        return None


# --- Auth boundary ---------------------------------------------------------


def _api_key_ok():
    key = request.headers.get("X-API-Key")
    return bool(MCP_API_KEY) and key == MCP_API_KEY


def require_auth(view):
    """Allow a request through if it carries a logged-in session OR the shared
    MCP API key. API-key callers are flagged on `g` so routes can attribute
    their output as unowned AI images."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if current_user.is_authenticated:
            g.api_key_auth = False
            return view(*args, **kwargs)
        if _api_key_ok():
            g.api_key_auth = True
            return view(*args, **kwargs)
        return jsonify({"error": "authentication required"}), 401

    return wrapper


def current_actor():
    """Resolve the caller to (user_id, origin) for storing on a generation.
    Session user -> (id, 'user'); MCP API key -> (None, 'ai')."""
    if getattr(g, "api_key_auth", False):
        return None, "ai"
    if current_user.is_authenticated:
        return current_user.id, "user"
    return None, "ai"


# --- Routes ----------------------------------------------------------------


def _render_login_page():
    from flask import current_app

    if current_user.is_authenticated:
        return redirect(url_for("index"))
    html = current_app.config["_LOGIN_HTML"].read_text()
    return Response(html, mimetype="text/html")


@auth_bp.route("/login", methods=["GET"])
def login_page():
    # Canonical login endpoint (login_manager.login_view points here).
    return _render_login_page()


@auth_bp.route("/register", methods=["GET"])
def register_page():
    # Same page; the template toggles to its register form client-side.
    return _render_login_page()


@auth_bp.route("/login", methods=["POST"])
def login_post():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    user = get_user_by_username(username)
    if not user or not user.password_hash or not check_password_hash(
        user.password_hash, password
    ):
        return _login_error("Invalid username or password.")
    login_user(user)
    return redirect(url_for("index"))


@auth_bp.route("/register", methods=["POST"])
def register_post():
    username = (request.form.get("username") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return _login_error("Username and password are required.")
    if get_user_by_username(username):
        return _login_error("That username is taken.")
    if email and get_user_by_email(email):
        return _login_error("That email is already registered.")
    try:
        user = create_local_user(username, email, password)
    except sqlite3.IntegrityError:
        return _login_error("That username or email is already registered.")
    login_user(user)
    return redirect(url_for("index"))


@auth_bp.route("/auth/google")
def google_login():
    if not GOOGLE_CLIENT_ID:
        return _login_error("Google sign-in is not configured.")
    redirect_uri = _google_redirect_uri()
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/google/callback")
def google_callback():
    if not GOOGLE_CLIENT_ID:
        return _login_error("Google sign-in is not configured.")
    token = oauth.google.authorize_access_token()
    info = token.get("userinfo") or {}
    sub = info.get("sub")
    if not sub:
        return _login_error("Google did not return an account id.")
    user = upsert_oauth_user("google", sub, info.get("email"))
    login_user(user)
    return redirect(url_for("index"))


@auth_bp.route("/logout", methods=["POST"])
def logout():
    logout_user()
    return redirect(url_for("auth.login_page"))


@auth_bp.route("/me")
def me():
    if not current_user.is_authenticated:
        return jsonify({"error": "not authenticated"}), 401
    return jsonify(
        {
            "username": current_user.display_name,
            "email": current_user.email,
        }
    )


# --- Helpers ---------------------------------------------------------------


def _login_error(message):
    """Re-render the login page with an error banner. Kept simple: inject the
    message into a placeholder in the static login template."""
    from flask import current_app

    html = current_app.config["_LOGIN_HTML"].read_text()
    banner = f'<div class="error-text">{message}</div>'
    html = html.replace("<!--ERROR-->", banner)
    return Response(html, mimetype="text/html", status=401)


def _google_redirect_uri():
    if OAUTH_REDIRECT_BASE:
        return f"{OAUTH_REDIRECT_BASE}/auth/google/callback"
    return url_for("auth.google_callback", _external=True)


def init_auth(app, login_html_path):
    """Wire Flask-Login + Authlib into the app and register the auth routes.
    `login_html_path` is a pathlib.Path to templates/login.html."""
    login_manager.init_app(app)
    login_manager.login_view = "auth.login_page"

    oauth.init_app(app)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url=GOOGLE_DISCOVERY_URL,
            client_kwargs={"scope": "openid email profile"},
        )

    app.config["_LOGIN_HTML"] = login_html_path
    app.register_blueprint(auth_bp)
