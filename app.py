from flask import (
    Flask, render_template, request, redirect, session,
    send_from_directory, Response, jsonify, abort
)
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
import queue
import re
import secrets
import signal
import threading
import time
import uuid
from datetime import timedelta


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

def _default_db_path():
    # Renamed from bobgram: keep using an existing legacy database so no
    # data is orphaned; fresh installs get the new name.
    return "bobgram.db" if os.path.exists("bobgram.db") else "bobworld.db"


DB_PATH = (
    os.environ.get("BOBWORLD_DB")
    or os.environ.get("BOBGRAM_DB")  # legacy env var, still honored
    or _default_db_path()
)
db = Database(DB_PATH, cipher)

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
# EVENT BROKER (SSE)
# ============================================================

class EventBroker:

    def __init__(self):
        self.lock = threading.RLock()

        self.global_clients = set()
        self.dm_clients = {}
        self.group_clients = {}

    def subscribe(self, chat_type, chat_id=None):
        q = queue.Queue()

        with self.lock:

            if chat_type == "global":
                self.global_clients.add(q)

            elif chat_type == "dm":
                self.dm_clients.setdefault(
                    chat_id,
                    set()
                ).add(q)

            elif chat_type == "group":
                self.group_clients.setdefault(
                    chat_id,
                    set()
                ).add(q)

        return q

    def unsubscribe(self, chat_type, q, chat_id=None):

        with self.lock:

            if chat_type == "global":
                self.global_clients.discard(q)

            elif chat_type == "dm":
                clients = self.dm_clients.get(chat_id)

                if clients:
                    clients.discard(q)

                    if not clients:
                        self.dm_clients.pop(
                            chat_id,
                            None
                        )

            elif chat_type == "group":
                clients = self.group_clients.get(chat_id)

                if clients:
                    clients.discard(q)

                    if not clients:
                        self.group_clients.pop(
                            chat_id,
                            None
                        )

    def publish(self, chat_type, data, chat_id=None):

        with self.lock:

            if chat_type == "global":
                clients = list(self.global_clients)

            elif chat_type == "dm":
                clients = list(self.dm_clients.get(chat_id, set()))

            elif chat_type == "group":
                clients = list(self.group_clients.get(chat_id, set()))

            else:
                return

        for client in clients:
            try:
                client.put_nowait(data)
            except Exception:
                self.unsubscribe(
                    chat_type,
                    client,
                    chat_id
                )


events = EventBroker()


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

    if request.endpoint in public_endpoints:
        return None

    if request.path.startswith("/uploads/"):
        return None

    if request.path.startswith("/avatars/"):
        return None

    if not session.get("user"):
        return redirect("/login")

    return None


# ============================================================
# AUTH
# ============================================================

@app.route("/")
def index():
    return redirect("/chat")


@app.route("/login", methods=["GET", "POST"])
def login():

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")

        user = db.get_user_full(username)

        if user and check_password_hash(
            user.get("password_hash", ""),
            password
        ):
            session.clear()
            session["user"] = username
            session.permanent = True

            return redirect("/chat")

        error = "Invalid username or password."

    return render_template(
        "login.html",
        error=error,
        username=request.form.get("username", ""),
    )


@app.route("/signup", methods=["GET", "POST"])
def signup():

    error = None

    if request.method == "POST":

        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        display_name = request.form.get("display_name", username).strip()
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
        elif len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            error = "Display name is too long (max 64 characters)."
        elif len(bio) > MAX_BIO_LENGTH:
            error = "Bio is too long (max 500 characters)."
        elif db.user_exists(username):
            error = "That username is already taken."

        if error is None:
            db.create_user(
                username,
                generate_password_hash(password),
                display_name,
                bio,
            )

            session.clear()
            session["user"] = username
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
        "username": username,
        "display_name": "",
        "bio": "",
        "avatar": None,
    }

    if request.method == "POST":

        display_name = request.form.get("display_name", username).strip()
        bio = request.form.get("bio", "").strip()

        if len(display_name) > MAX_DISPLAY_NAME_LENGTH:
            return "Display name too long", 400

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

        db.update_profile(username, display_name or username, bio, new_avatar)

        return redirect("/profile")

    return render_template(
        "profile.html",
        user=username,
        profile=user_data
    )


# ============================================================
# USER PROFILE
# ============================================================

@app.route("/user/<username>")
def view_user(username):

    profile = db.get_user(username)

    current = db.get_user(session["user"]) or {}

    if not profile:
        abort(404)

    return render_template(
        "user_profile.html",
        user=session["user"],
        display_name=current.get("display_name", session["user"]),
        avatar=current.get("avatar"),
        bio=current.get("bio", ""),
        profile=profile,
        username=username,
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
    events.publish(chat_type, message, chat_id)


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

    user_data = db.get_user(current) or {}

    messages = db.get_messages("global", "", 200)

    all_users = db.all_users()

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

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "status": "ok",
                "message": message
            })

        return redirect(f"/dm/{username}")

    messages = db.get_messages("dm", key, 200)

    all_users = db.all_users()

    current_user = db.get_user(me) or {}

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

    if chat_type == "group":
        allowed = (
            existing.get("from") == current
            or group.get("creator") == current
        )
    else:
        allowed = existing.get("from") == current

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

    events.publish(
        chat_type,
        {
            "type": "delete",
            "message_id": message_id,
        },
        chat_id if chat_type != "global" else None
    )

    return jsonify({"status": "ok"})


# ============================================================
# USERS
# ============================================================

@app.route("/users")
def users_list():

    current = session["user"]

    users_dict = db.all_users()

    users = [
        {
            "username": username,
            "display_name": data.get("display_name", username),
            "avatar": data.get("avatar"),
            "bio": data.get("bio", ""),
        }
        for username, data in users_dict.items()
        if username != current
    ]

    return render_template(
        "users.html",
        users=users,
        session_user=current,
        users_dict=users_dict,
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

        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({
                "status": "ok",
                "message": message
            })

        return redirect(f"/group/{group_id}")

    messages = db.get_messages("group", group_id, 200)

    users = db.all_users()

    user_data = db.get_user(current) or {}

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

    if group.get("creator") != current:
        return jsonify({"error": "Only creator can edit"}), 403

    db.update_group_name(group_id, name)

    events.publish(
        "group",
        {
            "type": "group_update",
            "name": name,
        },
        group_id
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

    if group.get("creator") != current:
        return jsonify({"error": "Only creator can delete"}), 403

    # Remove attachments belonging to the group.
    for filename in files:
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    events.publish(
        "group",
        {"type": "group_deleted"},
        group_id
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
# Deliberately simple: threads + replies, optional image per
# post, no live streaming (refresh to see new posts).
# ============================================================

MAX_THREAD_TITLE_LENGTH = 150
MAX_POST_BODY_LENGTH = 5_000


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

    threads = db.list_forum_threads()
    users = db.all_users()
    user_data = db.get_user(current) or {}

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

        db.create_forum_thread(thread_id, current, title, body, image)

        return redirect(f"/forum/{thread_id}")

    # The new-thread form now lives directly on the forum page.
    return redirect("/forum")


@app.route("/forum/<thread_id>")
def forum_thread(thread_id):

    current = session["user"]

    thread = db.get_forum_thread(thread_id)

    if not thread:
        abort(404)

    replies = db.get_forum_replies(thread_id)
    users = db.all_users()
    user_data = db.get_user(current) or {}

    return render_template(
        "forum_thread.html",
        section="forum",
        thread=thread,
        replies=replies,
        users=users,
        user=current,
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

    db.add_forum_reply(
        thread_id,
        uuid.uuid4().hex,
        current,
        body,
        image,
    )

    return redirect(f"/forum/{thread_id}")


@app.route("/forum/<thread_id>/delete", methods=["POST"])
def forum_delete_thread(thread_id):

    current = session["user"]

    thread = db.get_forum_thread(thread_id)

    if not thread:
        return jsonify({"error": "Thread not found"}), 404

    if thread["author"] != current:
        return jsonify({
            "error": "Only the author can delete this thread"
        }), 403

    for filename in db.delete_forum_thread(thread_id):
        path = os.path.join(UPLOAD_FOLDER, filename)
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return redirect("/forum")


@app.route("/forum/reply/<reply_id>/delete", methods=["POST"])
def forum_delete_reply(reply_id):

    current = session["user"]

    reply = db.get_forum_reply(reply_id)

    if not reply:
        return jsonify({"error": "Reply not found"}), 404

    if reply["author"] != current:
        return jsonify({
            "error": "Only the author can delete this reply"
        }), 403

    db.delete_forum_reply(reply_id)

    if reply.get("image"):
        path = os.path.join(UPLOAD_FOLDER, reply["image"])
        if os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    return redirect(f"/forum/{reply['thread_id']}")


# ============================================================
# SSE
# ============================================================

def sse_response(chat_type, chat_id=None):

    q = events.subscribe(chat_type, chat_id)

    def generate():

        try:

            yield (
                "data: "
                + json.dumps({"type": "connected"})
                + "\n\n"
            )

            while True:

                try:
                    event = q.get(timeout=25)

                    yield (
                        "data: "
                        + json.dumps(event, ensure_ascii=False)
                        + "\n\n"
                    )

                except queue.Empty:

                    # Keep proxies/load balancers
                    # from killing idle connections.
                    yield (
                        "data: "
                        + json.dumps({"type": "heartbeat"})
                        + "\n\n"
                    )

        except GeneratorExit:
            pass

        finally:
            events.unsubscribe(chat_type, q, chat_id)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.route("/stream/global")
def stream_global():

    if not session.get("user"):
        return "Unauthorized", 401

    return sse_response("global")


@app.route("/stream/dm/<username>")
def stream_dm(username):

    me = session["user"]

    if not db.user_exists(username):
        return "User not found", 404

    return sse_response("dm", dm_key(me, username))


@app.route("/stream/group/<group_id>")
def stream_group(group_id):

    current = session["user"]

    group = db.get_group(group_id)

    if not group:
        return "Group not found", 404

    if current not in group.get("members", []):
        return "Forbidden", 403

    return sse_response("group", group_id)


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

    # Tell connected SSE clients.
    shutdown_event = {"type": "shutdown"}

    with events.lock:

        clients = (
            list(events.global_clients)
            + [
                q
                for clients_set
                in events.dm_clients.values()
                for q in clients_set
            ]
            + [
                q
                for clients_set
                in events.group_clients.values()
                for q in clients_set
            ]
        )

    for q in clients:
        try:
            q.put_nowait(shutdown_event)
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
    app.run(
        host="0.0.0.0",
        port=80,
        threaded=True,
        use_reloader=False,
    )
