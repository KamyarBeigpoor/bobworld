"""
SQLite storage layer for bobworld.

Replaces the previous JSON-file persistence (users.json, messages.json,
groups.json, dm_conversations/*.json, group_messages/*.json).

- Single SQLite database file (default: bobworld.db), WAL mode.
- One connection shared across threads, guarded by an RLock
  (traffic is low and every operation is short).
- DM and group message text/reply fields are encrypted at rest with the
  same Fernet cipher the app used for its JSON conversation files.
  Global chat remains plaintext, mirroring the old behaviour.
- On first run (empty database) legacy JSON data is migrated in
  automatically. The original JSON files are left untouched as a backup.
"""

import glob as _glob
import json
import logging
import os
import sqlite3
import threading
import time
import uuid

log = logging.getLogger("bobworld.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    bio           TEXT NOT NULL DEFAULT '',
    avatar        TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    creator       TEXT NOT NULL,
    password_hash TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS group_members (
    group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE ON UPDATE CASCADE,
    username TEXT NOT NULL,
    PRIMARY KEY (group_id, username)
);

CREATE TABLE IF NOT EXISTS messages (
    id        TEXT PRIMARY KEY,
    chat_type TEXT NOT NULL CHECK (chat_type IN ('global', 'dm', 'group')),
    chat_id   TEXT NOT NULL DEFAULT '',
    sender    TEXT NOT NULL,
    text      TEXT NOT NULL DEFAULT '',
    file      TEXT,
    reply     TEXT,
    timestamp INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS forum_threads (
    id        TEXT PRIMARY KEY,
    author    TEXT NOT NULL,
    title     TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    image     TEXT,
    timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_forum_threads_ts
    ON forum_threads (timestamp DESC);

CREATE TABLE IF NOT EXISTS forum_replies (
    id        TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE ON UPDATE CASCADE,
    author    TEXT NOT NULL,
    body      TEXT NOT NULL DEFAULT '',
    image     TEXT,
    timestamp INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_forum_replies_thread
    ON forum_replies (thread_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_messages_chat
    ON messages (chat_type, chat_id, timestamp);
"""


def _row_to_user(row):
    if not row:
        return None
    return {
        "username": row["username"],
        "password_hash": row["password_hash"],
        "display_name": row["display_name"] or "",
        "bio": row["bio"] or "",
        "avatar": row["avatar"],
    }


class Database:

    def __init__(self, path, cipher):
        self.path = path
        self._cipher = cipher
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; we manage transactions manually
        )
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------
    # LOW LEVEL
    # ------------------------------------------------------------------

    def close(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self._conn.close()

    def _encrypt(self, value):
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return self._cipher.encrypt(raw).decode()

    def _decrypt(self, token):
        raw = self._cipher.decrypt(token.encode())
        return json.loads(raw.decode())

    # Encryption helpers (kept public so app.py can reuse the same
    # scheme for any additional sensitive fields in the future).

    def encrypt_field(self, value):
        return "enc:" + self._encrypt(value)

    def decrypt_field(self, stored):
        if stored is None:
            return None
        if isinstance(stored, str) and stored.startswith("enc:"):
            try:
                return self._decrypt(stored[4:])
            except Exception:
                log.exception("Failed to decrypt field; returning placeholder")
                return "[undecryptable]"
        return stored

    # ------------------------------------------------------------------
    # USERS
    # ------------------------------------------------------------------

    def get_user(self, username):
        """Public user dict (no password hash), or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        user = _row_to_user(row)
        if user is None:
            return None
        return {
            "username": user["username"],
            "display_name": user["display_name"],
            "bio": user["bio"],
            "avatar": user["avatar"],
        }

    def get_user_full(self, username):
        """User dict including password_hash (server-side use only)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
        return _row_to_user(row)

    def user_exists(self, username):
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM users WHERE username = ?", (username,)
            ).fetchone()
        return row is not None

    def create_user(self, username, password_hash, display_name, bio):
        with self._lock:
            self._conn.execute(
                "INSERT INTO users (username, password_hash, display_name, bio, avatar)"
                " VALUES (?, ?, ?, ?, NULL)",
                (username, password_hash, display_name, bio),
            )

    def update_profile(self, username, display_name, bio, new_avatar=None):
        with self._lock:
            if new_avatar is None:
                self._conn.execute(
                    "UPDATE users SET display_name = ?, bio = ? WHERE username = ?",
                    (display_name, bio, username),
                )
            else:
                self._conn.execute(
                    "UPDATE users SET display_name = ?, bio = ?, avatar = ?"
                    " WHERE username = ?",
                    (display_name, bio, new_avatar, username),
                )

    def all_users(self, include_password=False):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM users ORDER BY username COLLATE NOCASE"
            ).fetchall()
        users = {}
        for row in rows:
            user = _row_to_user(row)
            if not include_password:
                user.pop("password_hash")
            users[user["username"]] = user
        return users

    def count_users(self):
        with self._lock:
            row = self._conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return row["n"]

    def delete_user_avatar(self, username):
        """Returns previous avatar filename (or None) and clears it."""
        with self._lock:
            row = self._conn.execute(
                "SELECT avatar FROM users WHERE username = ?", (username,)
            ).fetchone()
            if not row:
                return None
            old = row["avatar"]
            self._conn.execute(
                "UPDATE users SET avatar = NULL WHERE username = ?", (username,)
            )
        return old

    # ------------------------------------------------------------------
    # MESSAGES
    # ------------------------------------------------------------------

    def add_message(self, chat_type, chat_id, message):
        """
        message: dict with keys id, from, text, file, timestamp and an
        optional 'reply' dict. Text/reply are encrypted for dm/group.
        """
        reply = message.get("reply")
        with self._lock:
            self._conn.execute(
                "INSERT INTO messages (id, chat_type, chat_id, sender, text,"
                " file, reply, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message["id"],
                    chat_type,
                    chat_id or "",
                    message["from"],
                    (
                        message.get("text", "")
                        if chat_type == "global"
                        else self.encrypt_field(message.get("text", ""))
                    ),
                    message.get("file"),
                    self.encrypt_field(reply) if reply is not None else None,
                    int(message["timestamp"]),
                ),
            )

    @staticmethod
    def _message_row_to_dict(row, decrypt_fn):
        reply_stored = row["reply"]
        reply = None
        if reply_stored:
            reply = decrypt_fn(reply_stored)
            if not isinstance(reply, dict):
                reply = None
        return {
            "id": row["id"],
            "from": row["sender"],
            "text": decrypt_fn(row["text"]) or "",
            "file": row["file"],
            "timestamp": row["timestamp"],
            **({"reply": reply} if reply else {}),
        }

    def get_messages(self, chat_type, chat_id="", limit=200):
        """Returns up to `limit` messages in chronological order."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE chat_type = ? AND chat_id = ?"
                " ORDER BY timestamp DESC, id DESC LIMIT ?",
                (chat_type, chat_id or "", limit),
            ).fetchall()
        rows = list(reversed(rows))
        return [self._message_row_to_dict(r, self.decrypt_field) for r in rows]

    def get_message(self, chat_type, chat_id, message_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM messages WHERE chat_type = ? AND chat_id = ?"
                " AND id = ?",
                (chat_type, chat_id or "", message_id),
            ).fetchone()
        if row is None:
            return None
        return self._message_row_to_dict(row, self.decrypt_field)

    def delete_message(self, chat_type, chat_id, message_id):
        """Deletes one message; returns the deleted dict (with file name)."""
        message = self.get_message(chat_type, chat_id, message_id)
        if message is None:
            return None
        with self._lock:
            self._conn.execute(
                "DELETE FROM messages WHERE id = ? AND chat_type = ? AND chat_id = ?",
                (message_id, chat_type, chat_id or ""),
            )
        return message

    def delete_chat_messages(self, chat_type, chat_id):
        """Deletes all messages of a chat; returns list of attachment filenames."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT file FROM messages WHERE chat_type = ? AND chat_id = ?",
                (chat_type, chat_id or ""),
            ).fetchall()
            self._conn.execute(
                "DELETE FROM messages WHERE chat_type = ? AND chat_id = ?",
                (chat_type, chat_id or ""),
            )
        return [r["file"] for r in rows if r["file"]]

    # ------------------------------------------------------------------
    # GROUPS
    # ------------------------------------------------------------------

    @staticmethod
    def _group_row_to_dict(row, members):
        return {
            "id": row["id"],
            "name": row["name"],
            "creator": row["creator"],
            "password_hash": row["password_hash"],
            "members": list(members),
        }

    def get_group(self, group_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            ).fetchone()
            if row is None:
                return None
            members = [
                r["username"]
                for r in self._conn.execute(
                    "SELECT username FROM group_members WHERE group_id = ?"
                    " ORDER BY username",
                    (group_id,),
                ).fetchall()
            ]
        return self._group_row_to_dict(row, members)

    def create_group(self, group_id, name, creator, password_hash):
        with self._lock:
            self._conn.execute(
                "INSERT INTO groups (id, name, creator, password_hash)"
                " VALUES (?, ?, ?, ?)",
                (group_id, name, creator, password_hash),
            )
            self._conn.execute(
                "INSERT INTO group_members (group_id, username) VALUES (?, ?)",
                (group_id, creator),
            )

    def update_group_name(self, group_id, name):
        with self._lock:
            self._conn.execute(
                "UPDATE groups SET name = ? WHERE id = ?", (name, group_id)
            )

    def add_group_member(self, group_id, username):
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO group_members (group_id, username)"
                " VALUES (?, ?)",
                (group_id, username),
            )

    def remove_group_member(self, group_id, username):
        with self._lock:
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id = ? AND username = ?",
                (group_id, username),
            )

    def delete_group(self, group_id):
        """Deletes group + membership rows + its messages.
        Returns (group_dict_or_None, [attachment filenames])."""
        group = self.get_group(group_id)
        if group is None:
            return None, []
        files = self.delete_chat_messages("group", group_id)
        with self._lock:
            self._conn.execute(
                "DELETE FROM group_members WHERE group_id = ?", (group_id,)
            )
            self._conn.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return group, files

    def all_groups(self):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM groups ORDER BY name COLLATE NOCASE"
            ).fetchall()
            member_rows = self._conn.execute(
                "SELECT group_id, username FROM group_members"
            ).fetchall()
        members_by_group = {}
        for r in member_rows:
            members_by_group.setdefault(r["group_id"], []).append(r["username"])
        return {
            row["id"]: self._group_row_to_dict(
                row, members_by_group.get(row["id"], [])
            )
            for row in rows
        }

    # ------------------------------------------------------------------
    # FORUM
    # ------------------------------------------------------------------

    @staticmethod
    def _forum_row_to_dict(row):
        post = {
            "id": row["id"],
            "author": row["author"],
            "body": row["body"] or "",
            "image": row["image"],
            "timestamp": row["timestamp"],
        }
        keys = row.keys()
        if "title" in keys:
            post["title"] = row["title"]
        if "thread_id" in keys:
            post["thread_id"] = row["thread_id"]
        return post

    def create_forum_thread(self, thread_id, author, title, body, image=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO forum_threads (id, author, title, body, image,"
                " timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (thread_id, author, title, body, image,
                 int(time.time() * 1000)),
            )

    def list_forum_threads(self, limit=100):
        with self._lock:
            rows = self._conn.execute(
                "SELECT t.*, COUNT(r.id) AS reply_count"
                " FROM forum_threads t"
                " LEFT JOIN forum_replies r ON r.thread_id = t.id"
                " GROUP BY t.id ORDER BY t.timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
        threads = []
        for row in rows:
            thread = self._forum_row_to_dict(row)
            thread["reply_count"] = row["reply_count"]
            threads.append(thread)
        return threads

    def get_forum_thread(self, thread_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forum_threads WHERE id = ?", (thread_id,)
            ).fetchone()
        return None if row is None else self._forum_row_to_dict(row)

    def get_forum_replies(self, thread_id):
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM forum_replies WHERE thread_id = ?"
                " ORDER BY timestamp ASC",
                (thread_id,),
            ).fetchall()
        return [self._forum_row_to_dict(r) for r in rows]

    def add_forum_reply(self, thread_id, reply_id, author, body, image=None):
        with self._lock:
            self._conn.execute(
                "INSERT INTO forum_replies (id, thread_id, author, body,"
                " image, timestamp) VALUES (?, ?, ?, ?, ?, ?)",
                (reply_id, thread_id, author, body, image,
                 int(time.time() * 1000)),
            )

    def get_forum_reply(self, reply_id):
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM forum_replies WHERE id = ?", (reply_id,)
            ).fetchone()
        return None if row is None else self._forum_row_to_dict(row)

    def delete_forum_reply(self, reply_id):
        """Returns deleted reply dict or None."""
        reply = self.get_forum_reply(reply_id)
        if reply is None:
            return None
        with self._lock:
            self._conn.execute(
                "DELETE FROM forum_replies WHERE id = ?", (reply_id,)
            )
        return reply

    def delete_forum_thread(self, thread_id):
        """Deletes thread + replies; returns list of image filenames."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT image FROM forum_threads WHERE id = ?"
                " UNION ALL"
                " SELECT image FROM forum_replies WHERE thread_id = ?",
                (thread_id, thread_id),
            ).fetchall()
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute(
                "DELETE FROM forum_threads WHERE id = ?", (thread_id,)
            )
        return [r["image"] for r in rows if r["image"]]

    # ------------------------------------------------------------------
    # LEGACY JSON MIGRATION
    # ------------------------------------------------------------------

    def migrate_legacy_if_empty(self, base_dir):
        """
        One-time import from the old JSON storage. Runs only when the
        users table is empty. Returns a stats dict describing what was
        imported. Raises RuntimeError if encrypted conversations cannot
        be decrypted with the current key (refuses to silently lose data).
        """
        if self.count_users() > 0:
            return {"skipped": True}

        stats = {
            "skipped": False,
            "users": 0,
            "groups": 0,
            "memberships": 0,
            "global_messages": 0,
            "dm_conversations": 0,
            "dm_messages": 0,
            "group_message_files": 0,
            "group_messages": 0,
            "ids_assigned": 0,
        }

        def load_json(path, default):
            if not os.path.exists(path):
                return default
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                log.exception("Migration: failed to read %s", path)
                return default

        def load_encrypted_or_plain(path):
            data = load_json(path, [])
            if isinstance(data, dict) and data.get("encrypted"):
                try:
                    raw = self._cipher.decrypt(data["data"].encode())
                    decoded = json.loads(raw.decode())
                except Exception:
                    raise RuntimeError(
                        f"Cannot decrypt legacy conversation '{path}' with the "
                        "current DM_ENCRYPTION_KEY. Restore the key that was "
                        "used when the file was written, or move the file away "
                        "to skip importing it."
                    )
                return decoded if isinstance(decoded, list) else []
            return data if isinstance(data, list) else []

        def norm_message(msg, chat_type):
            nonlocal stats
            mid = msg.get("id") or str(uuid.uuid4())
            if "id" not in msg:
                stats["ids_assigned"] += 1
            reply = msg.get("reply")
            if not isinstance(reply, dict):
                reply = None
            return {
                "id": mid,
                "from": str(msg.get("from", "")),
                "text": str(msg.get("text", "") or ""),
                "file": msg.get("file"),
                "timestamp": int(msg.get("timestamp", 0) or 0),
                "reply": reply,
            }

        dm_dir = os.path.join(base_dir, "dm_conversations")
        gm_dir = os.path.join(base_dir, "group_messages")

        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                # Users ---------------------------------------------------
                users = load_json(os.path.join(base_dir, "users.json"), {})
                if isinstance(users, dict):
                    for username, data in users.items():
                        if not isinstance(data, dict):
                            continue
                        self._conn.execute(
                            "INSERT OR IGNORE INTO users (username,"
                            " password_hash, display_name, bio, avatar)"
                            " VALUES (?, ?, ?, ?, ?)",
                            (
                                username,
                                data.get("password", ""),
                                data.get("display_name", "") or "",
                                data.get("bio", "") or "",
                                data.get("avatar"),
                            ),
                        )
                        stats["users"] += 1

                # Groups --------------------------------------------------
                groups = load_json(os.path.join(base_dir, "groups.json"), {})
                if isinstance(groups, dict):
                    for gid, group in groups.items():
                        if not isinstance(group, dict):
                            continue
                        self._conn.execute(
                            "INSERT OR IGNORE INTO groups (id, name, creator,"
                            " password_hash) VALUES (?, ?, ?, ?)",
                            (
                                gid,
                                group.get("name", "Unnamed group"),
                                group.get("creator", ""),
                                group.get("password_hash", ""),
                            ),
                        )
                        stats["groups"] += 1
                        for member in group.get("members", []):
                            self._conn.execute(
                                "INSERT OR IGNORE INTO group_members"
                                " (group_id, username) VALUES (?, ?)",
                                (gid, member),
                            )
                            stats["memberships"] += 1

                # Global messages -----------------------------------------
                global_msgs = load_json(
                    os.path.join(base_dir, "messages.json"), []
                )
                if isinstance(global_msgs, list):
                    for msg in global_msgs:
                        if not isinstance(msg, dict):
                            continue
                        m = norm_message(msg, "global")
                        self._conn.execute(
                            "INSERT OR IGNORE INTO messages (id, chat_type,"
                            " chat_id, sender, text, file, reply, timestamp)"
                            " VALUES (?, 'global', '', ?, ?, ?, ?, ?)",
                            (
                                m["id"], m["from"], m["text"], m["file"],
                                self.encrypt_field(m["reply"])
                                if m["reply"] else None,
                                m["timestamp"],
                            ),
                        )
                        stats["global_messages"] += 1

                # DM conversations ----------------------------------------
                for filepath in sorted(
                    _glob.glob(os.path.join(dm_dir, "dm_*.json"))
                ):
                    filename = os.path.basename(filepath)
                    chat_id = filename[3:-5]
                    msgs = load_encrypted_or_plain(filepath)
                    count = 0
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        m = norm_message(msg, "dm")
                        self._conn.execute(
                            "INSERT OR IGNORE INTO messages (id, chat_type,"
                            " chat_id, sender, text, file, reply, timestamp)"
                            " VALUES (?, 'dm', ?, ?, ?, ?, ?, ?)",
                            (
                                m["id"], chat_id, m["from"],
                                self.encrypt_field(m["text"]),
                                m["file"],
                                self.encrypt_field(m["reply"])
                                if m["reply"] else None,
                                m["timestamp"],
                            ),
                        )
                        count += 1
                    stats["dm_conversations"] += 1
                    stats["dm_messages"] += count

                # Group messages ------------------------------------------
                for filepath in sorted(
                    _glob.glob(os.path.join(gm_dir, "group_*.json"))
                ):
                    filename = os.path.basename(filepath)
                    chat_id = filename[6:-5]
                    msgs = load_encrypted_or_plain(filepath)
                    count = 0
                    for msg in msgs:
                        if not isinstance(msg, dict):
                            continue
                        m = norm_message(msg, "group")
                        self._conn.execute(
                            "INSERT OR IGNORE INTO messages (id, chat_type,"
                            " chat_id, sender, text, file, reply, timestamp)"
                            " VALUES (?, 'group', ?, ?, ?, ?, ?, ?)",
                            (
                                m["id"], chat_id, m["from"],
                                self.encrypt_field(m["text"]),
                                m["file"],
                                self.encrypt_field(m["reply"])
                                if m["reply"] else None,
                                m["timestamp"],
                            ),
                        )
                        count += 1
                    stats["group_message_files"] += 1
                    stats["group_messages"] += count

                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

        return stats
