from flask import (
    Flask, render_template, request, redirect, session,
    send_from_directory, jsonify, abort
)
from flask_socketio import SocketIO, join_room
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from database import Database

import base64
import atexit
import json
import os
import re
import secrets
import signal
import threading
import time
import uuid
from datetime import timedelta
from urllib.parse import urlparse


# ============================================================
# .ENV LOADING
#
# The previous version shipped a .env file but never loaded it,
# so FLASK_SECRET_KEY was regenerated on every restart (killing
# all sessions). This tiny loader has no external dependency.
# ============================================================

def load_env_file(path=".env"):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except OSError:
        pass


load_env_file()


# ============================================================
# APP
# ============================================================

app = Flask(__name__)

app.secret_key = os.environ.get(
    "FLASK_SECRET_KEY",
    secrets.token_hex(32)
)

app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)
# Uploaded files have immutable uuid names and css/js are versioned by
# query string, so aggressive browser caching is safe and cuts requests.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = timedelta(hours=12)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
# Set SESSION_COOKIE_SECURE=true in .env once the site is served over HTTPS.
if os.environ.get("SESSION_COOKIE_SECURE", "").lower() in ("1", "true", "yes"):
    app.config["SESSION_COOKIE_SECURE"] = True

# ============================================================
# DEPLOYMENT SELF-CHECK
#
# Fail immediately (with a readable list) if the app was not
# fully copied/deployed — e.g. a Render/Git checkout missing
# templates/ produces 'TemplateNotFound' 500s on every request,
# which is much harder to debug than this one clear error.
# ============================================================

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

REQUIRED_FILES = (
    # templates
    "templates/login.html",
    "templates/signup.html",
    "templates/chat.html",
    "templates/dm.html",
    "templates/users.html",
    "templates/groups.html",
    "templates/group_chat.html",
    "templates/join_group.html",
    "templates/forum.html",
    "templates/forum_thread.html",
    "templates/profile.html",
    "templates/user_profile.html",
    "templates/_topbar.html",
    # backend module
    "database.py",
    # stylesheets + fonts + scripts
    "static/css/chat.css",
    "static/css/win98.css",
    "static/css/videoplayer.css",
    "static/js/chat.js",
    "static/js/video-player.js",
    "static/js/socket.io.min.js",
    "static/css/ms_sans_serif.woff2",
    "static/css/ms_sans_serif_bold.woff2",
)

_missing = [
    name
    for name in REQUIRED_FILES
    if not os.path.exists(os.path.join(BASE_DIR, name))
]

if _missing:
    raise RuntimeError(
        "bobworld is missing required files on this server "
        "(the deployment did not include the full app folder):\n  - "
        + "\n  - ".join(_missing)
        + "\nCopy/commit the ENTIRE app folder (especially templates/ "
        "and static/) and redeploy."
    )


# ============================================================
# DIRECTORIES
# ============================================================

DM_FOLDER = "dm_conversations"       # legacy only; kept for migration
GROUP_MESSAGES_FOLDER = "group_messages"  # legacy only; kept for migration

UPLOAD_FOLDER = "static/uploads"
AVATAR_FOLDER = "static/avatars"

for directory in (
    UPLOAD_FOLDER,
    AVATAR_FOLDER,
):
    os.makedirs(directory, exist_ok=True)


# ============================================================
# ENCRYPTION
# ============================================================

def get_encryption_key():
    value = os.environ.get("DM_ENCRYPTION_KEY")

    if value:
        try:
            # Validate Fernet key.
            Fernet(value.encode())
            return value.encode()
        except Exception:
            app.logger.warning(
                "DM_ENCRYPTION_KEY is set but is not a valid Fernet key "
                "(it must be 44-char url-safe base64). Falling back to the "
                "development key so existing conversations stay readable. "
                "Generate a proper key with: "
                'python -c "from cryptography.fernet import Fernet; '
                'print(Fernet.generate_key().decode())"'
            )

    # Development fallback only.
    password = b"development-only-change-this"
    salt = b"development-salt"

    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        iterations=600_000,
    )

    return base64.urlsafe_b64encode(kdf.derive(password))


ENCRYPTION_KEY = get_encryption_key()
cipher = Fernet(ENCRYPTION_KEY)


# ============================================================
# DATABASE
# ============================================================
#
# Two backends are available behind the same Database API:
#
#   SQLite  (default)          - local file, zero config.
#   Supabase (PostgreSQL)      - set SUPABASE_DB_URL in .env to switch;
#                                run migrate_sqlite_to_supabase.py once
#                                to move existing data over.
#

def _default_db_path():
    # Renamed from bobgram: keep using an existing legacy database so no
    # data is orphaned; fresh installs get the new name.
    return "bobgram.db" if os.path.exists("bobgram.db") else "bobworld.db"


SUPABASE_DB_URL = (
    os.environ.get("SUPABASE_DB_URL")
    or os.environ.get("DATABASE_URL")
    or ""
).strip()

if SUPABASE_DB_URL:
    db = Database.open_supabase(SUPABASE_DB_URL, cipher)
else:
    DB_PATH = (
        os.environ.get("BOBWORLD_DB")
        or os.environ.get("BOBGRAM_DB")  # legacy env var, still honored
        or _default_db_path()
    )
    db = Database.open_sqlite(DB_PATH, cipher)

try:
    migration_stats = db.migrate_legacy_if_empty(os.path.dirname(
        os.path.abspath(__file__)
    ) or ".")
    if not migration_stats.get("skipped"):
        app.logger.info("Legacy JSON migration: %s", migration_stats)
except RuntimeError as exc:
    app.logger.error("Legacy JSON migration aborted: %s", exc)
except Exception:
    app.logger.exception("Legacy JSON migration failed")


# ============================================================
# LIMITS
# ============================================================

ALLOWED_UPLOAD_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
    "bmp",
    "mp4",
    "webm",
    "mov",
    "mp3",
    "wav",
    "ogg",
    "txt",
    "pdf",
}

ALLOWED_AVATAR_EXTENSIONS = {
    "png",
    "jpg",
    "jpeg",
    "gif",
    "webp",
}

MAX_MESSAGE_LENGTH = 10_000
MAX_USERNAME_LENGTH = 32
MAX_DISPLAY_NAME_LENGTH = 64
MAX_BIO_LENGTH = 500
MAX_GROUP_NAME_LENGTH = 100

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


# ============================================================
# REALTIME (Socket.IO)
#
# Replaces the old per-page SSE streams. One persistent WebSocket
# connection per tab now carries chat messages AND notifications,
# instead of two hanging HTTP responses. This matters because
# browsers allow only ~6 parallel HTTP/1.1 connections per host:
# a couple of open tabs used to starve the pool, making page
# navigations (e.g. the Friends tab) hang and the whole site feel
# slow. Socket.IO multiplexes everything over a single connection.
#
# Rooms:
#   chat:global            - every connected client (global chat page)
#   chat:dm:<dm_key>       - both participants of one DM conversation
#   chat:group:<group_id>  - members watching one group
#   user:<username>        - personal room: notifications, bans, etc.
# ============================================================

socketio = SocketIO(
    app,
    async_mode="threading",
    cors_allowed_origins=None,  # same-origin only
)
# The browser client library is vendored at static/js/socket.io.min.js
# (newer python-engineio releases no longer serve it from /socket.io/).


def chat_room(chat_type, chat_id=None):
    if chat_type == "global":
        return "chat:global"
    if chat_type == "dm":
        return f"chat:dm:{chat_id}"
    if chat_type == "group":
        return f"chat:group:{chat_id}"
    return None


def user_room(username):
    return f"user:{username}"


@socketio.on("connect")
def ws_connect():
    """Reject anonymous sockets; give each user a personal room."""
    me = session.get("user")
    if not me:
        return False  # refuse the connection
    join_room(user_room(me))


@socketio.on("subscribe")
def ws_subscribe(data):
    """
    The client asks to join its current chat's room after connecting
    (and again after any reconnect). Authorization mirrors what the
    old /stream/* endpoints enforced before opening an SSE pipe.
    """
    me = session.get("user")

    if not me or not isinstance(data, dict):
        return {"ok": False}

    chat_type = data.get("chat_type")

    try:
        if chat_type == "global":
            join_room(chat_room("global"))
            return {"ok": True}

        if chat_type == "dm":
            username = str(data.get("username") or "")
            if not username or username == me \
                    or not db.user_exists(username):
                return {"ok": False}
            join_room(chat_room("dm", dm_key(me, username)))
            return {"ok": True}

        if chat_type == "group":
            group_id = str(data.get("group_id") or "")
            group = db.get_group(group_id)
            if not group or me not in group.get("members", []):
                return {"ok": False}
            join_room(chat_room("group", group_id))
            return {"ok": True}

    except Exception:
        app.logger.exception("socket subscribe failed")

    return {"ok": False}


# ============================================================
# VALIDATION / UPLOADS
# ============================================================

def valid_extension(filename, allowed):
    if "." not in filename:
        return False

    extension = filename.rsplit(".", 1)[1].lower()

    return extension in allowed


def save_upload(file, avatar=False):

    if not file or not file.filename:
        return None

    filename = secure_filename(file.filename)

    if not filename:
        return None

    allowed = (
        ALLOWED_AVATAR_EXTENSIONS
        if avatar
        else ALLOWED_UPLOAD_EXTENSIONS
    )

    if not valid_extension(filename, allowed):
        return None

    extension = filename.rsplit(".", 1)[1].lower()

    generated = f"{uuid.uuid4().hex}.{extension}"

    folder = AVATAR_FOLDER if avatar else UPLOAD_FOLDER

    path = os.path.join(folder, generated)

    file.save(path)

    return generated


def validate_message(text):

    text = (text or "").strip()

    if len(text) > MAX_MESSAGE_LENGTH:
        return None

    return text


def sanitize_reply(reply_data):
    """
    Parse and whitelist reply payload from the client.

    The old code embedded whatever JSON arrived straight into the stored
    message, which every other client then rendered. Only known keys are
    kept now and lengths are capped.
    """
    if not reply_data:
        return None

    try:
        reply = json.loads(reply_data)
    except (ValueError, TypeError):
        return None

    if not isinstance(reply, dict):
        return None

    cleaned = {}

    sender = reply.get("from")
    if isinstance(sender, str):
        cleaned["from"] = sender[:MAX_USERNAME_LENGTH]

    text = reply.get("text")
    if isinstance(text, str):
        cleaned["text"] = text[:300]

    file = reply.get("file")
    if isinstance(file, str) and valid_extension(file, ALLOWED_UPLOAD_EXTENSIONS):
        cleaned["file"] = file[:200]

    timestamp = reply.get("timestamp")
    if isinstance(timestamp, int):
        cleaned["timestamp"] = timestamp

    return cleaned or None


# ============================================================
# SECURITY HELPERS
# ============================================================

def current_uid():
    """Immutable internal id of the logged-in account (or None)."""
    return session.get("uid")


def safe_redirect_target(target, fallback="/"):
    """Only allow same-site relative redirects (blocks open redirects)."""
    if target and target.startswith("/") and not target.startswith("//"):
        return target
    return fallback


def _same_origin_request():
    """
    True when there is solid browser evidence that this state-changing
    request originates from THIS site (anti-CSRF):

    - Sec-Fetch-Site: same-origin / none   (modern browsers), or
    - Origin header absent AND Referer host == request host
      (older browsers omit Sec-Fetch-Site; POSTs always carry Origin,
       so a missing Origin + matching Referer is still first-party).
    """
    sec_fetch = request.headers.get("Sec-Fetch-Site", "")
    if sec_fetch:
        return sec_fetch.lower() in ("same-origin", "same-site", "none")

    origin = request.headers.get("Origin")
    if origin:
        try:
            return urlparse(origin).netloc == request.host
        except ValueError:
            return False

    referer = request.headers.get("Referer")
    if referer:
        try:
            return bool(urlparse(referer).netloc) \
                and urlparse(referer).netloc == request.host
        except ValueError:
            return False

    # No browser signals at all: allow only token-authenticated calls.
    token = session.get("csrf_token")
    supplied = (
        request.headers.get("X-CSRF-Token")
        or request.form.get("csrf_token")
    )
    return bool(token and supplied and secrets.compare_digest(
        token, supplied
    ))


@app.before_request
def csrf_origin_guard():
    # Socket.IO transport frames are not application state changes; the
    # handshake itself is protected by the session cookie and by
    # Socket.IO's same-origin policy.
    if request.path.startswith("/socket.io"):
        return None
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        if not _same_origin_request():
            return jsonify({"error": "Cross-site request blocked"}), 403
    # Issue a per-session token so templates/JS can authenticate fetches
    # from clients that send no Origin/Sec-Fetch headers.
    if session.get("user") and not session.get("csrf_token"):
        session["csrf_token"] = secrets.token_hex(32)
    return None


@app.after_request
def security_headers(response):
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


# ============================================================
# AUTH MIDDLEWARE
# ============================================================

@app.before_request
def check_active_session():

    public_endpoints = {
        "login",
        "signup",
        "static",
        "uploads",
        "avatars",
    }

    # Socket.IO handles its own auth (anonymous sockets are rejected in
    # ws_connect) and must never be redirected to the login page.
    if request.path.startswith("/socket.io"):
        return None

    if request.endpoint in public_endpoints:
        return None

    if request.path.startswith("/uploads/"):
        return None

    if request.path.startswith("/avatars/"):
        return None

    username = session.get("user")

    if not username:
        return redirect("/login")

    # GHOST-ACCOUNT GUARD: the session must match a user who STILL
    # exists AND whose immutable id matches the one stored at login.
    # - database wiped/replaced  -> no such user -> force logout.
    # - account deleted, someone re-registers the same name -> their new
    #   id differs from our session id -> old sessions are dead.
    # (Single combined query keeps this fast on remote databases.)
    full, banned = db.get_user_with_ban(username)

    if full is None or full.get("id") != session.get("uid"):
        session.clear()
        return redirect("/login?deleted=1")

    # A banned user loses their session on the next request.
    if banned:
        session.clear()
        return redirect("/login?banned=1")

    return None


# ============================================================
# AUTH
# ============================================================

@app.route("/")
def index():
    return redirect("/chat")


# ============================================================
# LOGIN RATE LIMITING
#
# Small in-memory sliding window per IP+username. Enough to make
# password brute-forcing impractical without external dependencies.
# ============================================================

LOGIN_MAX_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 300
_login_attempts = {}
_login_lock = threading.Lock()


def _login_blocked(key):
    now = time.time()
    with _login_lock:
        window = [
            ts for ts in _login_attempts.get(key, ())
            if now - ts < LOGIN_WINDOW_SECONDS
        ]
        _login_attempts[key] = window
        return len(window) >= LOGIN_MAX_ATTEMPTS


def _login_record_failure(key):
    with _login_lock:
        _login_attempts.setdefault(key, []).append(time.time())


@app.route("/login", methods=["GET", "POST"])
def login():

    error = None

    # One-shot status messages arriving via query string.
    if request.args.get("banned"):
        error = "This account has been banned."
    elif request.args.get("deleted"):
        error = None  # shown as a success/info box below

    deleted = request.args.get("deleted") == "1"

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        key = f"{request.remote_addr}|{username.lower()}"

        if _login_blocked(key):
            error = (
                "Too many login attempts. Please wait a few minutes "
                "and try again."
            )
        else:
            user, banned = db.get_user_with_ban(username)

            if user and banned:
                error = "This account has been banned."
                _login_record_failure(key)
            elif user is not None:
                # Compute the password hash exactly once.
                if check_password_hash(
                    user.get("password_hash", ""),
                    password
                ):
                    session.clear()
                    session["user"] = username
                    # Bind the session to this account's immutable id.
                    session["uid"] = user.get("id")
                    session.permanent = True

                    return redirect("/chat")

                error = "Invalid username or password."
                _login_record_failure(key)
            else:
                error = "Invalid username or password."
                _login_record_failure(key)

    return render_template(
        "login.html",
        error=error,
        deleted=deleted,
        username=request.form.get("username", ""),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", "").strip()
        bio = request.form.get("bio", "").strip()

        if not username:
            error = "Username is required."
        elif len(username) > MAX_USERNAME_LENGTH:
            error = "Username is too long (max 32 characters)."
        elif not USERNAME_PATTERN.match(username):
            error = (
                "Username may only contain letters, numbers, "
                "dots, dashes and underscores."
            )
        elif not password:
            error = "Password is required."
        elif not display_name:
            error = "Display name is required."
        elif len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            error = "Display name is too long (max 64 characters)."
        elif len(bio) > MAX_BIO_LENGTH:
            error = "Bio is too long (max 500 characters)."
        elif db.user_exists(username):
            error = "That username is already taken."

        if error is None:
            new_id = uuid.uuid4().hex

            db.create_user(
                username,
                generate_password_hash(password),
                display_name,
                bio,
                user_id=new_id,
            )

            session.clear()
            session["user"] = username
            session["uid"] = new_id
            session.permanent = True

            return redirect("/chat")

    return render_template(
        "signup.html",
        error=error,
        username=username if request.method == "POST" else "",
        display_name=request.form.get("display_name", ""),
        bio=request.form.get("bio", ""),
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ============================================================
# PROFILE
# ============================================================

@app.route("/profile", methods=["GET", "POST"])
def profile():

    username = session["user"]

    user_data = db.get_user(username) or {
        "id": current_uid(),
        "username": username,
        "display_name": "",
        "bio": "",
        "avatar": None,
    }

    if request.method == "POST":

        # Display name is mandatory now.
        display_name = request.form.get("display_name", "").strip()

        if not display_name:
            return "Display name is required", 400

        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            return "Display name too long", 400

        bio = request.form.get("bio", "").strip()

        if len(bio) > MAX_BIO_LENGTH:
            return "Bio too long", 400

        new_avatar = None

        avatar = request.files.get("avatar")

        if avatar and avatar.filename:

            uploaded = save_upload(avatar, avatar=True)

            if not uploaded:
                return "Invalid avatar", 400

            old_avatar = user_data.get("avatar")

            if old_avatar:
                old_path = os.path.join(AVATAR_FOLDER, old_avatar)

                if os.path.exists(old_path):
                    try:
                        os.remove(old_path)
                    except OSError:
                        pass

            new_avatar = uploaded

        db.update_profile(username, display_name, bio, new_avatar)

        return redirect("/profile")

    return render_template(
        "profile.html",
        user=username,
        profile=user_data,
        pw_error=request.args.get("err"),
        pw_ok=request.args.get("ok"),
    )


# ============================================================
# USER PROFILE
# ============================================================

@app.route("/user/<username>")
def view_user(username):

    profile = db.get_user(username)

    if not profile:
        abort(404)

    current = db.get_user(session["user"]) or {}

    return render_template(
        "user_profile.html",
        user=session["user"],
        display_name=current.get("display_name", session["user"]),
        avatar=current.get("avatar"),
        bio=current.get("bio", ""),
        profile=profile,
        username=username,
        friend_status=db.friendship_status(session["user"], username),
        is_mod=is_moderator(session["user"]),
        target_is_mod=is_moderator(username),
        target_banned=db.is_banned(username),
    )


# ============================================================
# MESSAGE CREATION
# ============================================================

def create_message(sender, text, file):

    text = validate_message(text)

    if text is None:
        raise ValueError("Message is too long")

    filename = save_upload(file)

    if file and file.filename and not filename:
        raise ValueError("Invalid file")

    message = {
        "id": str(uuid.uuid4()),
        "from": sender,
        "sender_id": current_uid(),
        "text": text,
        "file": filename,
        "timestamp": int(time.time() * 1000),
    }

    reply = sanitize_reply(request.form.get("reply_data"))

    if reply:
        message["reply"] = reply

    if not message["text"] and not message["file"]:
        raise ValueError("Message cannot be empty")

    return message


def publish_message(chat_type, chat_id, message):
    room = chat_room(chat_type, chat_id)
    if room:
        socketio.emit("message", message, room=room)


def push_notification(username, ntype, actor, text, link):
    """
    Always record the notification as unread AND push it live to every
    open tab of the recipient (their personal socket room). Opening the
    conversation clears its notifications anyway.
    """
    nid = db.add_notification(username, ntype, actor, text, link)

    socketio.emit(
        "notification",
        {
            "type": "notification",
            "id": nid,
            "ntype": ntype,
            "actor": actor,
            "text": text,
            "link": link,
        },
        room=user_room(username),
    )


# ============================================================
# GLOBAL CHAT
# ============================================================

@app.route("/chat", methods=["GET", "POST"])
def chat():

    current = session["user"]

    if request.method == "POST":

        try:
            message = create_message(
                current,
                request.form.get("message"),
                request.files.get("file")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        db.add_message("global", "", message)

        publish_message("global", None, message)

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "status": "ok",
                "message": message
            })

        return redirect("/chat")

    messages = db.get_messages("global", "", 200)

    all_users = db.all_users()

    # Reuse the fetched directory for our own sidebar data (saves a
    # round trip on remote databases like Supabase).
    user_data = all_users.get(current) or {}

    return render_template(
        "chat.html",
        messages=list(reversed(messages)),
        user=current,
        display_name=user_data.get("display_name", current),
        avatar=user_data.get("avatar"),
        bio=user_data.get("bio", ""),
        users=all_users,
    )


# ============================================================
# DM
# ============================================================

@app.route("/dm/<username>", methods=["GET", "POST"])
def dm(username):

    me = session["user"]

    if me == username:
        return redirect("/chat")

    other = db.get_user(username)

    if not other:
        abort(404)

    key = dm_key(me, username)

    if request.method == "POST":

        try:
            message = create_message(
                me,
                request.form.get("message"),
                request.files.get("file")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        db.add_message("dm", key, message)

        publish_message("dm", key, message)

        # Always record; badge only if they are not watching this chat.
        preview = message.get("text") or "📎 sent an attachment"
        push_notification(
            username,
            "dm",
            me,
            preview[:80],
            f"/dm/{me}",
        )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "status": "ok",
                "message": message
            })

        return redirect(f"/dm/{username}")

    messages = db.get_messages("dm", key, 200)

    # Opening the conversation clears its notifications.
    db.mark_notifications_read(me, link=f"/dm/{username}")

    all_users = db.all_users()

    current_user = all_users.get(me) or {}

    return render_template(
        "dm.html",
        messages=list(reversed(messages)),
        user=me,
        other=username,
        other_display=other.get("display_name", username),
        other_avatar=other.get("avatar"),
        users=all_users,
        bio=current_user.get("bio", ""),
        display_name=current_user.get("display_name", me),
        avatar=current_user.get("avatar"),
    )


def dm_key(a, b):
    return "__".join(sorted((a, b)))


# ============================================================
# DELETE MESSAGE
# ============================================================

@app.route("/delete_message", methods=["POST"])
def delete_message():

    data = request.get_json(silent=True) or {}

    message_id = data.get("message_id")
    chat_type = data.get("chat_type")
    chat_id = data.get("chat_id") or ""

    if not message_id:
        return jsonify({"error": "Missing message_id"}), 400

    current = session["user"]

    if chat_type not in ("global", "dm", "group"):
        return jsonify({"error": "Invalid chat type"}), 400

    if chat_type == "dm" and not chat_id:
        return jsonify({"error": "Missing chat_id"}), 400

    if chat_type == "global":
        chat_id = ""

    group = None

    if chat_type == "group":

        group = db.get_group(chat_id)

        if not group:
            return jsonify({"error": "Group not found"}), 404

        if current not in group.get("members", []):
            return jsonify({"error": "Not a member"}), 403

    # Authorize BEFORE deleting so a forbidden request cannot destroy data.
    existing = db.get_message(chat_type, chat_id, message_id)

    if existing is None:
        return jsonify({"error": "Message not found"}), 404

    moderator = is_moderator(current)
    uid = current_uid()

    # Ownership is decided by the stored SENDER ID (immune to username
    # reuse); the username comparison only covers legacy rows that were
    # never backfilled.
    owns = (
        existing.get("sender_id") == uid
        if existing.get("sender_id")
        else existing.get("from") == current
    )

    if chat_type == "group":
        allowed = (
            owns
            or group.get("creator") == current
            or group.get("creator_id") == uid
            or moderator
        )
    else:
        allowed = owns or moderator

    if not allowed:
        return jsonify({
            "error": "Cannot delete others' messages"
        }), 403

    deleted = db.delete_message(chat_type, chat_id, message_id)

    if deleted is None:
        return jsonify({"error": "Message not found"}), 404

    filename = deleted.get("file")

    if filename:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    socketio.emit(
        "delete",
        {
            "type": "delete",
            "message_id": message_id,
        },
        room=chat_room(chat_type, chat_id if chat_type != "global" else None),
    )

    return jsonify({"status": "ok"})


# ============================================================
# USERS
# ============================================================

@app.route("/users")
def users_list():

    current = session["user"]

    users_dict = db.all_users()

    banned_set = db.all_bans()

    users = []
    for username, data in users_dict.items():
        if username == current:
            continue
        users.append({
            "username": username,
            "display_name": data.get("display_name", username),
            "avatar": data.get("avatar"),
            "bio": data.get("bio", ""),
            "friend_status": db.friendship_status(current, username),
            "is_mod": is_moderator(username),
        })

    return render_template(
        "users.html",
        users=users,
        session_user=current,
        users_dict=users_dict,
        is_mod=is_moderator(current),
        banned_set=banned_set,
    )


# ============================================================
# GROUP LIST
# ============================================================

@app.route("/groups")
def groups_list_page():

    current = session["user"]

    groups_raw = db.all_groups()

    groups = []

    for gid, group in groups_raw.items():
        groups.append({
            "id": gid,
            "name": group.get("name", "Unnamed group"),
            "creator": group.get("creator"),
            "member_count": len(group.get("members", [])),
            "is_member": current in group.get("members", []),
        })

    users = db.all_users()

    return render_template(
        "groups.html",
        user=current,
        groups=groups,
        users=users,
    )


# ============================================================
# CREATE GROUP
# ============================================================

@app.route("/group/create", methods=["GET", "POST"])
def create_group():

    current = session["user"]

    if request.method == "POST":

        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")

        if not name or not password:
            return redirect("/groups")

        if len(name) > MAX_GROUP_NAME_LENGTH:
            return "Group name too long", 400

        group_id = uuid.uuid4().hex

        db.create_group(
            group_id,
            name,
            current,
            current_uid(),
            generate_password_hash(password),
        )

        return redirect(f"/group/{group_id}")

    # The create form now lives directly on the groups page.
    return redirect("/groups")


# ============================================================
# JOIN GROUP
# ============================================================

@app.route("/group/<group_id>/join", methods=["POST"])
def join_group(group_id):

    password = request.form.get("password", "")
    current = session["user"]

    group = db.get_group(group_id)

    if not group:
        return "Group not found", 404

    if not check_password_hash(
        group.get("password_hash", ""),
        password
    ):
        return render_template(
            "join_group.html",
            group=group,
            user=current,
            error="Incorrect password. Please try again.",
        ), 403

    db.add_group_member(group_id, current)

    return redirect(f"/group/{group_id}")


# ============================================================
# GROUP CHAT
# ============================================================

@app.route("/group/<group_id>", methods=["GET", "POST"])
def group_chat(group_id):

    current = session["user"]

    group = db.get_group(group_id)

    if not group:
        abort(404)

    if current not in group.get("members", []):
        return render_template(
            "join_group.html",
            group=group,
            user=current
        )

    if request.method == "POST":

        try:
            message = create_message(
                current,
                request.form.get("message"),
                request.files.get("file")
            )
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        db.add_message("group", group_id, message)

        publish_message("group", group_id, message)

        # Always record per member; badge only if they are not watching.
        preview = message.get("text") or "📎 sent an attachment"
        for member in group.get("members", []):
            if member == current:
                continue
            push_notification(
                member,
                "group",
                current,
                f"#{group['name']}: {preview[:70]}",
                f"/group/{group_id}",
            )

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "status": "ok",
                "message": message
            })

        return redirect(f"/group/{group_id}")

    messages = db.get_messages("group", group_id, 200)

    # Opening the group clears its notifications.
    db.mark_notifications_read(current, link=f"/group/{group_id}")

    users = db.all_users()

    user_data = users.get(current) or {}

    # Never expose the group's password hash to the client.
    safe_group = {
        "id": group["id"],
        "name": group["name"],
        "creator": group["creator"],
        "members": group["members"],
    }

    return render_template(
        "group_chat.html",
        group=safe_group,
        messages=list(reversed(messages)),
        user=current,
        display_name=user_data.get("display_name", current),
        avatar=user_data.get("avatar"),
        bio=user_data.get("bio", ""),
        users=users,
    )


# ============================================================
# EDIT GROUP
# ============================================================

@app.route("/group/<group_id>/edit", methods=["POST"])
def edit_group(group_id):

    current = session["user"]

    name = request.form.get("name", "").strip()

    if not name:
        return jsonify({"error": "Name required"}), 400

    if len(name) > MAX_GROUP_NAME_LENGTH:
        return jsonify({"error": "Name too long"}), 400

    group = db.get_group(group_id)

    if not group:
        return jsonify({"error": "Group not found"}), 404

    uid = current_uid()

    owns_group = (
        group.get("creator") == current or group.get("creator_id") == uid
    )

    if not owns_group:
        return jsonify({"error": "Only creator can edit"}), 403

    db.update_group_name(group_id, name)

    socketio.emit(
        "group_update",
        {
            "type": "group_update",
            "name": name,
        },
        room=chat_room("group", group_id),
    )

    return jsonify({
        "status": "ok",
        "name": name,
    })


# ============================================================
# DELETE GROUP
# ============================================================

@app.route("/group/<group_id>/delete", methods=["POST"])
def delete_group(group_id):

    current = session["user"]

    group, files = db.delete_group(group_id)

    if group is None:
        return jsonify({"error": "Group not found"}), 404

    uid = current_uid()

    if group.get("creator") != current \
            and group.get("creator_id") != uid:
        return jsonify({"error": "Only creator can delete"}), 403

    # Remove attachments belonging to the group.
    for filename in files:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    socketio.emit(
        "group_deleted",
        {"type": "group_deleted"},
        room=chat_room("group", group_id),
    )

    return jsonify({"status": "ok"})


# ============================================================
# LEAVE GROUP
# ============================================================

@app.route("/group/<group_id>/leave", methods=["POST"])
def leave_group(group_id):

    current = session["user"]

    group = db.get_group(group_id)

    if not group:
        return jsonify({"error": "Group not found"}), 404

    if current not in group.get("members", []):
        return jsonify({"error": "Not a member"}), 400

    if group.get("creator") == current:
        return jsonify({
            "error": "Creator cannot leave; delete group instead"
        }), 400

    db.remove_group_member(group_id, current)

    return jsonify({"status": "ok"})


# ============================================================
# FORUM
#
# Classic board layout: threads with like/dislike scores, and
# reddit-style NESTED replies (reply-to-reply). Optional image per
# post; no live streaming (refresh to see new posts).
# ============================================================

MAX_THREAD_TITLE_LENGTH = 150
MAX_POST_BODY_LENGTH = 5_000
MAX_REPLY_DEPTH = 10

# Moderators can ban/unban users, delete accounts and delete any message.
MODERATORS = {
    m.strip()
    for m in os.environ.get("BOBWORLD_MODS", "bob").split(",")
    if m.strip()
}


def is_moderator(username):
    return username in MODERATORS


@app.template_filter("ts")
def ts_format(ms):
    """Server-side 'YYYY-MM-DD HH:MM' for forum posts."""
    if not ms:
        return ""
    t = time.localtime(int(ms) / 1000)
    return time.strftime("%Y-%m-%d %H:%M", t)


def _forum_post_body():
    """Validates shared reply/thread body + image fields.
    Returns (body, image_filename_or_None, error_response_or_None)."""
    body = (request.form.get("body") or "").strip()

    if len(body) > MAX_POST_BODY_LENGTH:
        return None, None, ("Post is too long", 400)

    image_file = request.files.get("image")

    if image_file and image_file.filename:
        image = save_upload(image_file)
        if not image:
            return None, None, ("Invalid image", 400)
    else:
        image = None

    return body, image, None


@app.route("/forum")
def forum():

    current = session["user"]

    threads = db.list_forum_threads(viewer=current)
    users = db.all_users()
    user_data = users.get(current) or {}

    return render_template(
        "forum.html",
        section="forum",
        threads=threads,
        users=users,
        user=current,
        display_name=user_data.get("display_name", current),
        avatar=user_data.get("avatar"),
        bio=user_data.get("bio", ""),
    )


@app.route("/forum/new", methods=["GET", "POST"])
def forum_new():

    current = session["user"]

    if request.method == "POST":

        title = (request.form.get("title") or "").strip()

        if not title:
            return redirect("/forum")

        if len(title) > MAX_THREAD_TITLE_LENGTH:
            return "Title too long", 400

        body, image, error = _forum_post_body()

        if error:
            return error

        if not body and not image:
            return redirect("/forum")

        thread_id = uuid.uuid4().hex

        db.create_forum_thread(
            thread_id, current, current_uid(), title, body, image
        )

        return redirect(f"/forum/{thread_id}")

    # The new-thread form now lives directly on the forum page.
    return redirect("/forum")


@app.route("/forum/<thread_id>")
def forum_thread(thread_id):

    current = session["user"]

    thread = db.get_forum_thread(thread_id, viewer=current)

    if not thread:
        abort(404)

    replies = db.get_forum_replies(thread_id, viewer=current)
    users = db.all_users()
    user_data = users.get(current) or {}

    return render_template(
        "forum_thread.html",
        section="forum",
        thread=thread,
        replies=replies,
        users=users,
        user=current,
        is_mod=is_moderator(current),
        display_name=user_data.get("display_name", current),
        avatar=user_data.get("avatar"),
        bio=user_data.get("bio", ""),
    )


@app.route("/forum/<thread_id>/reply", methods=["POST"])
def forum_reply(thread_id):

    current = session["user"]

    if not db.get_forum_thread(thread_id):
        abort(404)

    body, image, error = _forum_post_body()

    if error:
        return error

    if not body and not image:
        return "Reply cannot be empty", 400

    parent_id = (request.form.get("parent_id") or "").strip() or None

    if parent_id:
        parent = db.get_forum_reply(parent_id)

        # The parent must belong to THIS thread.
        if not parent or parent["thread_id"] != thread_id:
            return "Invalid parent reply", 400

        # Cap nesting depth so the tree stays readable.
        if db.get_reply_depth(parent_id) >= MAX_REPLY_DEPTH:
            return "Reply nesting is too deep here", 400

    db.add_forum_reply(
        thread_id,
        parent_id,
        uuid.uuid4().hex,
        current,
        current_uid(),
        body,
        image,
    )

    return redirect(f"/forum/{thread_id}")


@app.route("/forum/vote", methods=["POST"])
def forum_vote():

    current = session["user"]

    data = request.get_json(silent=True) or {}

    target_type = data.get("target_type")
    target_id = data.get("target_id")
    value = data.get("value")

    if target_type not in ("thread", "reply"):
        return jsonify({"error": "Invalid target_type"}), 400

    if value not in (-1, 0, 1):
        return jsonify({"error": "Invalid value"}), 400

    exists = (
        db.get_forum_thread(target_id)
        if target_type == "thread"
        else db.get_forum_reply(target_id)
    )

    if not exists:
        return jsonify({"error": "Not found"}), 404

    summary = db.set_vote(target_type, target_id, current, value)

    return jsonify(summary)


@app.route("/forum/<thread_id>/delete", methods=["POST"])
def forum_delete_thread(thread_id):

    current = session["user"]

    thread = db.get_forum_thread(thread_id)

    if not thread:
        return jsonify({"error": "Thread not found"}), 404

    uid = current_uid()
    owns = (
        thread["author_id"] == uid
        if thread.get("author_id")
        else thread["author"] == current
    )

    if not owns and not is_moderator(current):
        return jsonify({
            "error": "Only the author or a moderator can delete this thread"
        }), 403

    for filename in db.delete_forum_thread(thread_id):
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return redirect(request.form.get("next") or "/forum")


@app.route("/forum/reply/<reply_id>/delete", methods=["POST"])
def forum_delete_reply(reply_id):

    current = session["user"]

    reply = db.get_forum_reply(reply_id)

    if not reply:
        return jsonify({"error": "Reply not found"}), 404

    uid = current_uid()
    owns = (
        reply["author_id"] == uid
        if reply.get("author_id")
        else reply["author"] == current
    )

    if not owns and not is_moderator(current):
        return jsonify({
            "error": "Only the author or a moderator can delete this reply"
        }), 403

    deleted = db.delete_forum_reply(reply_id)

    for filename in [reply.get("image")] + (
        deleted.get("_deleted_images", []) if deleted else []
    ):
        if not filename:
            continue
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return redirect(f"/forum/{reply['thread_id']}")


# ============================================================
# FRIENDS
# ============================================================

@app.route("/friends")
def friends_page():

    me = session["user"]

    users = db.all_users()

    friends = []
    for friend in db.list_friends(me):
        info = users.get(friend, {})
        friends.append({
            "username": friend,
            "display_name": info.get("display_name", friend),
            "avatar": info.get("avatar"),
        })

    incoming = [
        {
            **req,
            "display_name": users.get(req["requester"], {}).get(
                "display_name", req["requester"]
            ),
        }
        for req in db.list_pending_incoming(me)
    ]

    outgoing = [
        {
            **req,
            "display_name": users.get(req["addressee"], {}).get(
                "display_name", req["addressee"]
            ),
        }
        for req in db.list_pending_outgoing(me)
    ]

    user_data = users.get(me) or {}

    return render_template(
        "friends.html",
        section="friends",
        friends=friends,
        incoming=incoming,
        outgoing=outgoing,
        user=me,
        display_name=user_data.get("display_name", me),
        avatar=user_data.get("avatar"),
        bio=user_data.get("bio", ""),
    )


@app.route("/friends/request/<username>", methods=["POST"])
def friend_request_route(username):

    me = session["user"]

    if not db.user_exists(username):
        return jsonify({"error": "User not found"}), 404

    try:
        result = db.send_friend_request(me, username)
    except ValueError as exc:
        return redirect("/users")

    if result == "pending_out":
        push_notification(
            username,
            "friend_request",
            me,
            f"{me} sent you a friend request.",
            "/friends",
        )

    return redirect(safe_redirect_target(
        request.form.get("next"), "/users"
    ))


@app.route("/friends/accept/<path:req_ref>", methods=["POST"])
def friend_accept_route(req_ref):
    """req_ref: '<requester>|<addressee>'."""

    me = session["user"]

    parts = req_ref.split("|", 1)

    if len(parts) != 2:
        return redirect("/friends")

    requester, addressee = parts

    if addressee != me:
        return redirect("/friends")

    req = db.get_friend_request(req_ref)

    if not req or req["addressee"] != me or req["status"] != "pending":
        return redirect("/friends")

    db.accept_friend_request(requester, addressee)

    push_notification(
        requester,
        "friend_accepted",
        me,
        f"{me} accepted your friend request.",
        f"/user/{me}",
    )

    return redirect("/friends")


@app.route("/friends/decline/<path:req_ref>", methods=["POST"])
def friend_decline_route(req_ref):
    """req_ref: '<requester>|<addressee>'. Works for declining incoming
    AND cancelling outgoing requests."""

    me = session["user"]

    parts = req_ref.split("|", 1)

    if len(parts) != 2:
        return redirect("/friends")

    requester, addressee = parts

    req = db.get_friend_request(req_ref)

    # Works for declining incoming AND cancelling outgoing requests.
    if not req or (req["addressee"] != me and req["requester"] != me):
        return redirect("/friends")

    db.decline_friend_request(requester, addressee)

    return redirect("/friends")


@app.route("/friends/remove/<username>", methods=["POST"])
def friend_remove_route(username):

    me = session["user"]

    db.remove_friend(me, username)

    return redirect(safe_redirect_target(
        request.form.get("next"), "/friends"
    ))


# ============================================================
# NOTIFICATIONS
# ============================================================

@app.route("/notifications")
def notifications_api():

    me = session["user"]

    return jsonify({
        "unread": db.unread_notification_count(me),
        "items": db.list_notifications(me),
    })


@app.route("/notifications/read", methods=["POST"])
def notifications_read_api():

    me = session["user"]

    data = request.get_json(silent=True) or {}

    if data.get("id") is not None:
        db.mark_notifications_read(me, notif_id=int(data["id"]))
    else:
        db.mark_notifications_read(me)  # mark all

    return jsonify({"status": "ok"})


# ============================================================
# ACCOUNT: PASSWORD & DELETE
# ============================================================

@app.route("/profile/password", methods=["POST"])
def profile_password():

    me = session["user"]
    uid = current_uid()

    current_pw = request.form.get("current_password", "")
    new_pw = request.form.get("new_password", "")
    confirm_pw = request.form.get("confirm_password", "")

    user_full = db.get_user_full(me)

    if not user_full or not check_password_hash(
        user_full.get("password_hash", ""),
        current_pw
    ):
        return redirect("/profile?err=Current+password+is+incorrect.")

    # Guard against a re-registered username hijacking this route.
    if not uid or user_full.get("id") != uid:
        session.clear()
        return redirect("/login?deleted=1")

    if len(new_pw) < 4:
        return redirect("/profile?err=New+password+must+be+at+least+4+characters.")

    if new_pw != confirm_pw:
        return redirect("/profile?err=New+passwords+do+not+match.")

    db.change_password(me, generate_password_hash(new_pw), expect_id=uid)

    return redirect("/profile?ok=Password+updated.")


@app.route("/profile/delete", methods=["POST"])
def profile_delete():

    me = session["user"]
    uid = current_uid()

    # Require the current password so a hijacked tab cannot nuke the
    # account in one click.
    password = request.form.get("password", "")

    user_full = db.get_user_full(me)

    if not user_full or not check_password_hash(
        user_full.get("password_hash", ""),
        password
    ):
        return redirect(
            "/profile?err=Password+required+to+delete+the+account."
        )

    result = db.delete_user_account(me, expect_id=uid)

    if result is None:
        session.clear()
        return redirect("/login?deleted=1")

    # Remove owned files from disk.
    files = [result.get("avatar")]
    files += result.get("forum_images", [])
    files += result.get("group_images", [])

    for filename in files:
        if not filename:
            continue
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    socketio.emit(
        "account_deleted",
        {"type": "account_deleted"},
        room=user_room(me),
    )

    session.clear()

    return redirect("/login?deleted=1")


# ============================================================
# MODERATION
#
# Users listed in MODERATORS (default: "bob") may ban/unban users,
# delete accounts and delete any message.
# ============================================================

def _mod_guard():
    """Returns an error response when the caller is not a moderator."""
    if not is_moderator(session["user"]):
        return jsonify({"error": "Moderator access required"}), 403
    return None


def _unlink_upload(filename):
    if not filename:
        return
    path = os.path.join(UPLOAD_FOLDER, filename)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


@app.route("/mod/ban/<username>", methods=["POST"])
def mod_ban(username):

    guard = _mod_guard()

    if guard:
        return guard

    if username == session["user"]:
        return jsonify({"error": "You cannot ban yourself"}), 400

    if is_moderator(username):
        return jsonify({"error": "Cannot ban a moderator"}), 400

    if not db.user_exists(username):
        return jsonify({"error": "User not found"}), 404

    reason = request.form.get("reason", "").strip()[:200]

    db.ban_user(username, reason)

    # Kick them out of any open tabs right away.
    socketio.emit(
        "banned",
        {"type": "banned"},
        room=user_room(username),
    )

    return redirect(safe_redirect_target(
        request.form.get("next"), "/users"
    ))


@app.route("/mod/unban/<username>", methods=["POST"])
def mod_unban(username):

    guard = _mod_guard()

    if guard:
        return guard

    db.unban_user(username)

    return redirect(safe_redirect_target(
        request.form.get("next"), "/users"
    ))


@app.route("/mod/delete_user/<username>", methods=["POST"])
def mod_delete_user(username):

    guard = _mod_guard()

    if guard:
        return guard

    if username == session["user"]:
        return jsonify({"error": "You cannot delete your own account here;"
                        " use Profile → Delete Account"}), 400

    if is_moderator(username):
        return jsonify({"error": "Cannot delete a moderator"}), 400

    if not db.user_exists(username):
        return jsonify({"error": "User not found"}), 404

    result = db.delete_user_account(username)

    if result is None:
        return jsonify({"error": "User not found"}), 404

    _unlink_upload(result.get("avatar"))

    for filename in result.get("forum_images", []):
        _unlink_upload(filename)

    for filename in result.get("group_images", []):
        _unlink_upload(filename)

    # Tell their open tabs the account ceased to exist.
    socketio.emit(
        "account_deleted",
        {"type": "account_deleted"},
        room=user_room(username),
    )

    return redirect(safe_redirect_target(
        request.form.get("next"), "/users"
    ))


# ============================================================
# STATIC FILES
# ============================================================

@app.route("/uploads/<path:name>")
def uploads(name):
    return send_from_directory(UPLOAD_FOLDER, name)


@app.route("/avatars/<path:filename>")
def avatars(filename):
    return send_from_directory(AVATAR_FOLDER, filename)


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(413)
def request_too_large(error):
    return jsonify({"error": "File too large"}), 413


# ============================================================
# SHUTDOWN
# ============================================================

_shutdown_lock = threading.Lock()
_shutdown_started = False


def shutdown_cleanly(signum=None, frame=None):

    global _shutdown_started

    with _shutdown_lock:

        if _shutdown_started:
            return

        _shutdown_started = True

    print("Shutting down...")

    # Tell connected Socket.IO clients.
    try:
        socketio.emit("shutdown", {"type": "shutdown"})
    except Exception:
        pass

    # SQLite writes are committed synchronously; just close cleanly.
    try:
        db.close()
    except Exception:
        pass

    print("Shutdown complete.")

    if signum is not None:
        raise SystemExit(0)


atexit.register(shutdown_cleanly)

signal.signal(signal.SIGINT, shutdown_cleanly)

try:
    signal.signal(signal.SIGTERM, shutdown_cleanly)
except (ValueError, AttributeError):
    # Not available on this platform.
    pass


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    # socketio.run() (not app.run) so WebSocket upgrades are served.
    socketio.run(
        app,
        host="0.0.0.0",
        port=3000,
        debug=False,
        use_reloader=False,
        allow_unsafe_werkzeug=True,  # dev server; same as before
    )
