"""
Storage layer for bobworld.

Two interchangeable backends behind one Database API:

1. SQLite   (default) - single file (bobworld.db), WAL mode.
2. Supabase           - PostgreSQL via SUPABASE_DB_URL (psycopg2 pool).
                        Point SUPABASE_DB_URL at your Supabase connection
                        string (Project Settings -> Database -> Connection
                        string) and the app switches automatically.
                        Run migrate_sqlite_to_supabase.py to move data.

Common properties:
- DM/group message text/reply fields stay encrypted at rest with Fernet.
- Every user has an immutable internal ID (`users.id`). Sessions carry
  this ID, so a brand-new account re-using a deleted account's username
  can never inherit its sessions or its content ownership.
- Message/forum/group rows remember the OWNER'S ID alongside the name,
  so ownership checks survive username reuse.
- Forum supports likes/dislikes (`forum_votes`) and reddit-style nested
  replies (`forum_replies.parent_id`).
"""

import glob as _glob
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

log = logging.getLogger("bobworld.db")


# ======================================================================
# SCHEMA (portable statements - executed one by one on both backends)
# ======================================================================

SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS users (
        username      TEXT NOT NULL,
        id            TEXT,
        password_hash TEXT NOT NULL,
        display_name  TEXT NOT NULL DEFAULT '',
        bio           TEXT NOT NULL DEFAULT '',
        avatar        TEXT,
        PRIMARY KEY (username)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS groups (
        id            TEXT PRIMARY KEY,
        name          TEXT NOT NULL,
        creator       TEXT NOT NULL,
        creator_id    TEXT,
        password_hash TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS group_members (
        group_id TEXT NOT NULL REFERENCES groups(id) ON DELETE CASCADE ON UPDATE CASCADE,
        username TEXT NOT NULL,
        PRIMARY KEY (group_id, username)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        id        TEXT PRIMARY KEY,
        chat_type TEXT NOT NULL CHECK (chat_type IN ('global', 'dm', 'group')),
        chat_id   TEXT NOT NULL DEFAULT '',
        sender    TEXT NOT NULL,
        sender_id TEXT,
        text      TEXT NOT NULL DEFAULT '',
        file      TEXT,
        reply     TEXT,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forum_threads (
        id        TEXT PRIMARY KEY,
        author    TEXT NOT NULL,
        author_id TEXT,
        title     TEXT NOT NULL,
        body      TEXT NOT NULL DEFAULT '',
        image     TEXT,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forum_replies (
        id        TEXT PRIMARY KEY,
        thread_id TEXT NOT NULL REFERENCES forum_threads(id) ON DELETE CASCADE ON UPDATE CASCADE,
        parent_id TEXT REFERENCES forum_replies(id) ON DELETE CASCADE ON UPDATE CASCADE,
        author    TEXT NOT NULL,
        author_id TEXT,
        body      TEXT NOT NULL DEFAULT '',
        image     TEXT,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS forum_votes (
        target_type TEXT NOT NULL CHECK (target_type IN ('thread', 'reply')),
        target_id   TEXT NOT NULL,
        username    TEXT NOT NULL,
        value       INTEGER NOT NULL CHECK (value IN (-1, 1)),
        timestamp   INTEGER NOT NULL,
        PRIMARY KEY (target_type, target_id, username)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS friendships (
        requester TEXT NOT NULL,
        addressee TEXT NOT NULL,
        status    TEXT NOT NULL CHECK (status IN ('pending', 'accepted')),
        timestamp INTEGER NOT NULL,
        PRIMARY KEY (requester, addressee)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bans (
        username  TEXT PRIMARY KEY,
        reason    TEXT,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notifications (
        nid       INTEGER NOT NULL DEFAULT 0,
        username  TEXT NOT NULL,
        ntype     TEXT NOT NULL,
        actor     TEXT,
        text      TEXT NOT NULL DEFAULT '',
        link      TEXT NOT NULL DEFAULT '',
        read      INTEGER NOT NULL DEFAULT 0,
        timestamp INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notification_seq (
        name TEXT PRIMARY KEY,
        seq  INTEGER NOT NULL
    )
    """,
]

INDEX_STATEMENTS = [
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_id_unique ON users (id)",
    "CREATE INDEX IF NOT EXISTS idx_forum_threads_ts ON forum_threads (timestamp DESC)",
    "CREATE INDEX IF NOT EXISTS idx_forum_replies_thread ON forum_replies (thread_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_forum_replies_parent ON forum_replies (parent_id)",
    "CREATE INDEX IF NOT EXISTS idx_forum_votes_target ON forum_votes (target_type, target_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_chat ON messages (chat_type, chat_id, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_friendships_requester ON friendships (requester, status)",
    "CREATE INDEX IF NOT EXISTS idx_friendships_addressee ON friendships (addressee, status)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_user ON notifications (username, read, timestamp)",
    "CREATE INDEX IF NOT EXISTS idx_notifications_nid ON notifications (nid)",
]


def _new_id():
    return uuid.uuid4().hex


# ======================================================================
# BACKENDS
# ======================================================================

class SQLiteBackend:
    """Single shared connection; guarded by the Database RLock."""

    def __init__(self, path):
        self.path = path
        self.is_postgres = False
        self._conn = sqlite3.connect(
            path,
            check_same_thread=False,
            isolation_level=None,  # autocommit; transactions are manual
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._tx_depth = 0

    def execute(self, sql, params=()):
        return self._conn.execute(sql, tuple(params))

    def query(self, sql, params=()):
        return [dict(r) for r in self.execute(sql, params).fetchall()]

    def query_one(self, sql, params=()):
        row = self.execute(sql, params).fetchone()
        return dict(row) if row else None

    @staticmethod
    def or_ignore(sql):
        return sql.replace("INSERT INTO", "INSERT OR IGNORE INTO", 1)

    @contextmanager
    def transaction(self):
        """Nested transactions join the outermost one."""
        self._tx_depth += 1
        try:
            if self._tx_depth == 1:
                self._conn.execute("BEGIN IMMEDIATE")
            yield
            self._tx_depth -= 1
            if self._tx_depth == 0:
                self._conn.execute("COMMIT")
        except Exception:
            if self._tx_depth > 0:
                self._tx_depth -= 1
            if self._tx_depth == 0:
                try:
                    self._conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
            raise

    def close(self):
        try:
            self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except sqlite3.Error:
            pass
        self._conn.close()


class PostgresBackend:
    """Supabase / PostgreSQL via psycopg2 connection pool.

    Connections are acquired per operation and returned immediately;
    transactions pin one connection for their duration.
    """

    def __init__(self, dsn, minconn=1, maxconn=10):
        try:
            import psycopg2.pool
            from psycopg2.extras import RealDictCursor
            from psycopg2.extensions import parse_dsn
        except ImportError as exc:
            raise RuntimeError(
                "SUPABASE_DB_URL is set but psycopg2 is not installed. "
                "Install it with: pip install psycopg2-binary"
            ) from exc

        self.is_postgres = True
        self._real_dict_cursor = RealDictCursor

        opts = parse_dsn(dsn)
        opts.setdefault(
            "sslmode", os.environ.get("SUPABASE_SSLMODE", "require")
        )
        self._pool = psycopg2.pool.ThreadedConnectionPool(
            minconn, maxconn, **opts
        )
        self._local = threading.local()

    @contextmanager
    def _conn(self):
        pinned = getattr(self._local, "pinned", None)
        if pinned is not None:
            yield pinned
            return
        conn = self._pool.getconn()
        try:
            conn.autocommit = True
            yield conn
        finally:
            self._pool.putconn(conn)

    def _execute(self, sql, params):
        sql = sql.replace("?", "%s")
        with self._conn() as conn:
            with conn.cursor(cursor_factory=self._real_dict_cursor) as cur:
                cur.execute(sql, tuple(params))
                if cur.description is None:
                    return []
                return [dict(r) for r in cur.fetchall()]

    def execute(self, sql, params=()):
        self._execute(sql, params)

    def query(self, sql, params=()):
        return self._execute(sql, params)

    def query_one(self, sql, params=()):
        rows = self._execute(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def or_ignore(sql):
        return sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    @contextmanager
    def transaction(self):
        """Pins a pooled connection; nested transactions join the outer."""
        pinned = getattr(self._local, "pinned", None)
        if pinned is not None:
            yield
            return
        conn = self._pool.getconn()
        self._local.pinned = conn
        try:
            conn.autocommit = False
            yield
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            self._local.pinned = None
            conn.autocommit = True
            self._pool.putconn(conn)

    def close(self):
        try:
            self._pool.closeall()
        except Exception:
            pass


# ======================================================================
# DATABASE
# ======================================================================

class Database:

    def __init__(self, backend, cipher):
        self.backend = backend
        self._cipher = cipher
        self._lock = threading.RLock()
        self._create_schema()

    # ------------------------------------------------------------------
    # CONSTRUCTION HELPERS
    # ------------------------------------------------------------------

    @classmethod
    def open_sqlite(cls, path, cipher):
        return cls(SQLiteBackend(path), cipher)

    @classmethod
    def open_supabase(cls, dsn, cipher):
        return cls(PostgresBackend(dsn), cipher)

    # ------------------------------------------------------------------
    # LOW LEVEL
    # ------------------------------------------------------------------

    def close(self):
        with self._lock:
            self.backend.close()

    def _encrypt(self, value):
        raw = json.dumps(
            value, ensure_ascii=False, separators=(",", ":")
        ).encode()
        return self._cipher.encrypt(raw).decode()

    def decrypt_field(self, stored):
        if stored is None:
            return None
        if isinstance(stored, str) and stored.startswith("enc:"):
            try:
                raw = self._cipher.decrypt(stored[4:].encode())
                return json.loads(raw.decode())
            except Exception:
                log.exception("Failed to decrypt field; returning placeholder")
                return "[undecryptable]"
        return stored

    # ------------------------------------------------------------------
    # SCHEMA SETUP + MIGRATION OF EXISTING DATABASES
    # ------------------------------------------------------------------

    def _create_schema(self):
        with self._lock:
            for stmt in SCHEMA_STATEMENTS:
                self.backend.execute(stmt)
            if not self.backend.is_postgres:
                self._sqlite_upgrade_old_database()
            for stmt in INDEX_STATEMENTS:
                self.backend.execute(stmt)
            self._backfill_owner_ids()

    def _sqlite_table_columns(self, table):
        rows = self.backend.query(
            "SELECT name FROM pragma_table_info(?)", (table,)
        )
        return {r["name"] for r in rows}

    def _sqlite_upgrade_old_database(self):
        """
        Brings databases created by bobworld <= 6.x up to the current
        schema: owner-id columns, the notification counter and ids.
        """
        additions = {
            "users": [("id", "TEXT")],
            "messages": [("sender_id", "TEXT")],
            "groups": [("creator_id", "TEXT")],
            "forum_threads": [("author_id", "TEXT")],
            "forum_replies": [("author_id", "TEXT"), ("parent_id", "TEXT")],
            "notifications": [("nid", "INTEGER NOT NULL DEFAULT 0")],
        }
        with self.backend.transaction():
            for table, columns in additions.items():
                existing = self._sqlite_table_columns(table)
                for column, coltype in columns:
                    if column not in existing:
                        self.backend.execute(
                            f"ALTER TABLE {table} ADD COLUMN"
                            f" {column} {coltype}"
                        )
            self.backend.execute(
                "INSERT OR IGNORE INTO notification_seq (name, seq)"
                " VALUES ('notifications', 0)"
            )
            self.backend.execute(
                "UPDATE notifications SET nid = rowid WHERE nid = 0"
            )
            self.backend.execute(
                "UPDATE notification_seq SET seq ="
                " (SELECT COALESCE(MAX(nid), 0) FROM notifications)"
                " WHERE name = 'notifications'"
            )

    def _backfill_owner_ids(self):
        """
        Give every user an immutable id and stamp every content row with
        its owner's id. Runs at every startup; a no-op once done.
        """
        with self._lock:
            with self.backend.transaction():
                for row in self.backend.query(
                    "SELECT username FROM users WHERE id IS NULL"
                ):
                    self.backend.execute(
                        "UPDATE users SET id = ? WHERE username = ?",
                        (_new_id(), row["username"]),
                    )

                # Stamp ownership onto content using the CURRENT owner of
                # each name. Afterwards ownership checks compare IDs, so
                # an account reusing the username inherits nothing.
                self.backend.execute(
                    "UPDATE messages SET sender_id ="
                    " (SELECT id FROM users WHERE username = messages.sender)"
                    " WHERE sender_id IS NULL"
                )
                self.backend.execute(
                    "UPDATE forum_threads SET author_id ="
                    " (SELECT id FROM users"
                    "  WHERE username = forum_threads.author)"
                    " WHERE author_id IS NULL"
                )
                self.backend.execute(
                    "UPDATE forum_replies SET author_id ="
                    " (SELECT id FROM users"
                    "  WHERE username = forum_replies.author)"
                    " WHERE author_id IS NULL"
                )
                self.backend.execute(
                    "UPDATE groups SET creator_id ="
                    " (SELECT id FROM users WHERE username = groups.creator)"
                    " WHERE creator_id IS NULL"
                )

    # ------------------------------------------------------------------
    # USERS
    # ------------------------------------------------------------------

    @staticmethod
    def _user_public(row):
        if not row:
            return None
        return {
            "id": row.get("id"),
            "username": row["username"],
            "display_name": row.get("display_name") or "",
            "bio": row.get("bio") or "",
            "avatar": row.get("avatar"),
        }

    @staticmethod
    def _user_full(row):
        if not row:
            return None
        user = Database._user_public(row)
        user["password_hash"] = row.get("password_hash", "")
        return user

    def get_user(self, username):
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM users WHERE username = ?", (username,)
            )
        return self._user_public(row)

    def get_user_full(self, username):
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM users WHERE username = ?", (username,)
            )
        return self._user_full(row)

    def get_user_full_by_id(self, user_id):
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            )
        return self._user_full(row)

    def user_exists(self, username):
        with self._lock:
            row = self.backend.query_one(
                "SELECT 1 AS ok FROM users WHERE username = ?", (username,)
            )
        return row is not None

    def create_user(self, username, password_hash, display_name, bio,
                    user_id=None):
        with self._lock:
            self.backend.execute(
                "INSERT INTO users (id, username, password_hash,"
                " display_name, bio, avatar)"
                " VALUES (?, ?, ?, ?, ?, NULL)",
                (user_id or _new_id(), username, password_hash,
                 display_name, bio),
            )

    def update_profile(self, username, display_name, bio, new_avatar=None):
        # Callers must verify session uid == account uid first; sessions
        # are uid-bound, which already blocks re-registered usernames.
        with self._lock:
            if new_avatar is None:
                self.backend.execute(
                    "UPDATE users SET display_name = ?, bio = ?"
                    " WHERE username = ?",
                    (display_name, bio, username),
                )
            else:
                self.backend.execute(
                    "UPDATE users SET display_name = ?, bio = ?, avatar = ?"
                    " WHERE username = ?",
                    (display_name, bio, new_avatar, username),
                )

    def all_users(self, include_password=False):
        with self._lock:
            rows = self.backend.query(
                "SELECT * FROM users ORDER BY LOWER(username)"
            )
        users = {}
        for row in rows:
            user = self._user_public(row)
            if include_password:
                user["password_hash"] = row.get("password_hash", "")
            users[user["username"]] = user
        return users

    def count_users(self):
        with self._lock:
            row = self.backend.query_one(
                "SELECT COUNT(*) AS n FROM users"
            )
        return row["n"]

    def delete_user_avatar(self, username):
        """Returns previous avatar filename (or None) and clears it."""
        with self._lock:
            row = self.backend.query_one(
                "SELECT avatar FROM users WHERE username = ?", (username,)
            )
            if not row:
                return None
            old = row["avatar"]
            self.backend.execute(
                "UPDATE users SET avatar = NULL WHERE username = ?",
                (username,),
            )
        return old

    # ------------------------------------------------------------------
    # MESSAGES
    # ------------------------------------------------------------------

    def add_message(self, chat_type, chat_id, message):
        """
        message: dict with keys id, from, sender_id, text, file, timestamp
        and an optional 'reply' dict. Text/reply are encrypted for dm/group.
        """
        reply = message.get("reply")
        with self._lock:
            self.backend.execute(
                "INSERT INTO messages (id, chat_type, chat_id, sender,"
                " sender_id, text, file, reply, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    message["id"],
                    chat_type,
                    chat_id or "",
                    message["from"],
                    message.get("sender_id"),
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
        reply_stored = row.get("reply")
        reply = None
        if reply_stored:
            reply = decrypt_fn(reply_stored)
            if not isinstance(reply, dict):
                reply = None
        msg = {
            "id": row["id"],
            "from": row["sender"],
            "sender_id": row.get("sender_id"),
            "text": decrypt_fn(row["text"]) or "",
            "file": row["file"],
            "timestamp": row["timestamp"],
        }
        if reply:
            msg["reply"] = reply
        return msg

    def get_messages(self, chat_type, chat_id="", limit=200):
        """Returns up to `limit` messages in chronological order."""
        with self._lock:
            rows = self.backend.query(
                "SELECT * FROM messages WHERE chat_type = ? AND chat_id = ?"
                " ORDER BY timestamp DESC, id DESC LIMIT ?",
                (chat_type, chat_id or "", limit),
            )
        rows.reverse()
        return [self._message_row_to_dict(r, self.decrypt_field)
                for r in rows]

    def get_message(self, chat_type, chat_id, message_id):
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM messages WHERE chat_type = ? AND chat_id = ?"
                " AND id = ?",
                (chat_type, chat_id or "", message_id),
            )
        if row is None:
            return None
        return self._message_row_to_dict(row, self.decrypt_field)

    def delete_message(self, chat_type, chat_id, message_id):
        """Deletes one message; returns the deleted dict (with file name)."""
        message = self.get_message(chat_type, chat_id, message_id)
        if message is None:
            return None
        with self._lock:
            self.backend.execute(
                "DELETE FROM messages WHERE id = ? AND chat_type = ?"
                " AND chat_id = ?",
                (message_id, chat_type, chat_id or ""),
            )
        return message

    def delete_chat_messages(self, chat_type, chat_id):
        """Deletes all messages of a chat; returns attachment filenames."""
        with self._lock:
            rows = self.backend.query(
                "SELECT file FROM messages WHERE chat_type = ? AND chat_id = ?",
                (chat_type, chat_id or ""),
            )
            self.backend.execute(
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
            "creator_id": row.get("creator_id"),
            "password_hash": row["password_hash"],
            "members": list(members),
        }

    def get_group(self, group_id):
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM groups WHERE id = ?", (group_id,)
            )
            if row is None:
                return None
            members = [
                r["username"]
                for r in self.backend.query(
                    "SELECT username FROM group_members WHERE group_id = ?"
                    " ORDER BY LOWER(username)",
                    (group_id,),
                )
            ]
        return self._group_row_to_dict(row, members)

    def create_group(self, group_id, name, creator, creator_id, password_hash):
        with self._lock:
            with self.backend.transaction():
                self.backend.execute(
                    "INSERT INTO groups (id, name, creator, creator_id,"
                    " password_hash) VALUES (?, ?, ?, ?, ?)",
                    (group_id, name, creator, creator_id, password_hash),
                )
                self.backend.execute(
                    "INSERT INTO group_members (group_id, username)"
                    " VALUES (?, ?)",
                    (group_id, creator),
                )

    def update_group_name(self, group_id, name):
        with self._lock:
            self.backend.execute(
                "UPDATE groups SET name = ? WHERE id = ?", (name, group_id)
            )

    def add_group_member(self, group_id, username):
        with self._lock:
            self.backend.execute(
                self.backend.or_ignore(
                    "INSERT INTO group_members (group_id, username)"
                    " VALUES (?, ?)"
                ),
                (group_id, username),
            )

    def remove_group_member(self, group_id, username):
        with self._lock:
            self.backend.execute(
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
            self.backend.execute(
                "DELETE FROM group_members WHERE group_id = ?", (group_id,)
            )
            self.backend.execute("DELETE FROM groups WHERE id = ?", (group_id,))
        return group, files

    def all_groups(self):
        with self._lock:
            rows = self.backend.query(
                "SELECT * FROM groups ORDER BY LOWER(name)"
            )
            member_rows = self.backend.query(
                "SELECT group_id, username FROM group_members"
            )
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
    # FORUM: vote helpers
    # ------------------------------------------------------------------

    _VOTE_TABLES = {"thread": "forum_threads", "reply": "forum_replies"}

    def _vote_select(self, target_type):
        table = self._VOTE_TABLES[target_type]
        return (
            "(SELECT COALESCE(SUM(v.value), 0) FROM forum_votes v"
            f"  WHERE v.target_type = '{target_type}'"
            f"  AND v.target_id = {table}.id) AS score,"
            " (SELECT COUNT(*) FROM forum_votes v"
            f"  WHERE v.target_type = '{target_type}'"
            f"  AND v.target_id = {table}.id AND v.value = 1)"
            " AS likes,"
            " (SELECT COUNT(*) FROM forum_votes v"
            f"  WHERE v.target_type = '{target_type}'"
            f"  AND v.target_id = {table}.id AND v.value = -1)"
            " AS dislikes"
        )

    def _my_votes_map(self, target_type, ids, viewer):
        """viewer -> {target_id: value}; empty when viewer is None."""
        if not viewer or not ids:
            return {}
        marks = ",".join("?" for _ in ids)
        rows = self.backend.query(
            f"SELECT target_id, value FROM forum_votes"
            f" WHERE target_type = ? AND username = ?"
            f" AND target_id IN ({marks})",
            (target_type, viewer, *ids),
        )
        return {r["target_id"]: r["value"] for r in rows}

    # ------------------------------------------------------------------
    # FORUM: threads
    # ------------------------------------------------------------------

    def create_forum_thread(self, thread_id, author, author_id, title, body,
                            image=None):
        with self._lock:
            self.backend.execute(
                "INSERT INTO forum_threads (id, author, author_id, title,"
                " body, image, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (thread_id, author, author_id, title, body, image,
                 int(time.time() * 1000)),
            )

    def list_forum_threads(self, viewer=None, limit=100):
        with self._lock:
            rows = self.backend.query(
                "SELECT forum_threads.*,"
                " (SELECT COUNT(*) FROM forum_replies r"
                "   WHERE r.thread_id = forum_threads.id) AS reply_count,"
                " (SELECT MAX(r2.timestamp) FROM forum_replies r2"
                "   WHERE r2.thread_id = forum_threads.id) AS last_activity,"
                f" {self._vote_select('thread')}"
                " FROM forum_threads"
                " ORDER BY forum_threads.timestamp DESC LIMIT ?",
                (limit,),
            )
            my_votes = self._my_votes_map(
                "thread", [r["id"] for r in rows], viewer
            )
        threads = []
        for row in rows:
            thread = dict(row)
            thread["last_activity"] = thread.get("last_activity") \
                or thread["timestamp"]
            thread["my_vote"] = my_votes.get(thread["id"], 0)
            threads.append(thread)
        return threads

    def get_forum_thread(self, thread_id, viewer=None):
        with self._lock:
            row = self.backend.query_one(
                "SELECT forum_threads.*,"
                f" {self._vote_select('thread')}"
                " FROM forum_threads WHERE forum_threads.id = ?",
                (thread_id,),
            )
        if row is None:
            return None
        thread = dict(row)
        thread["my_vote"] = self._my_votes_map(
            "thread", [thread_id], viewer
        ).get(thread_id, 0)
        return thread

    # ------------------------------------------------------------------
    # FORUM: replies (nested, reddit-style)
    # ------------------------------------------------------------------

    def get_forum_replies(self, thread_id, viewer=None):
        """
        Returns top-level replies as a nested tree:
        [{...fields..., likes, dislikes, score, my_vote, children: [...]}]
        Ordered oldest-first at every level.
        """
        with self._lock:
            rows = self.backend.query(
                "SELECT forum_replies.*,"
                f" {self._vote_select('reply')}"
                " FROM forum_replies WHERE thread_id = ?"
                " ORDER BY timestamp ASC, id ASC",
                (thread_id,),
            )
            my_votes = self._my_votes_map(
                "reply", [r["id"] for r in rows], viewer
            )

        nodes = {}
        for row in rows:
            node = dict(row)
            node["my_vote"] = my_votes.get(node["id"], 0)
            node["children"] = []
            nodes[node["id"]] = node

        roots = []
        for node in nodes.values():
            parent = nodes.get(node.get("parent_id"))
            if parent is None:
                roots.append(node)
            else:
                parent["children"].append(node)
        return roots

    def add_forum_reply(self, thread_id, parent_id, reply_id, author,
                        author_id, body, image=None):
        with self._lock:
            self.backend.execute(
                "INSERT INTO forum_replies (id, thread_id, parent_id, author,"
                " author_id, body, image, timestamp)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (reply_id, thread_id, parent_id, author, author_id, body,
                 image, int(time.time() * 1000)),
            )

    def get_forum_reply(self, reply_id, viewer=None):
        with self._lock:
            row = self.backend.query_one(
                "SELECT forum_replies.*,"
                f" {self._vote_select('reply')}"
                " FROM forum_replies WHERE forum_replies.id = ?",
                (reply_id,),
            )
        if row is None:
            return None
        reply = dict(row)
        reply["my_vote"] = self._my_votes_map(
            "reply", [reply_id], viewer
        ).get(reply_id, 0)
        return reply

    def get_reply_depth(self, reply_id):
        """Depth of a reply in its tree (top-level reply = 1)."""
        depth = 0
        current = reply_id
        with self._lock:
            while current is not None and depth < 100:
                row = self.backend.query_one(
                    "SELECT parent_id FROM forum_replies WHERE id = ?",
                    (current,),
                )
                if row is None:
                    break
                current = row["parent_id"]
                depth += 1
        return depth

    def _collect_reply_subtree(self, reply_id):
        """Returns ([subtree reply ids incl. itself], {image filenames})."""
        collected = []
        images = set()
        frontier = [reply_id]
        seen = {reply_id}
        with self._lock:
            while frontier:
                collected.extend(frontier)
                marks = ",".join("?" for _ in frontier)
                children = self.backend.query(
                    f"SELECT id, image FROM forum_replies"
                    f" WHERE parent_id IN ({marks})",
                    frontier,
                )
                for r in children:
                    if r["image"]:
                        images.add(r["image"])
                nxt = [r["id"] for r in children if r["id"] not in seen]
                seen.update(nxt)
                frontier = nxt
        return collected, images

    def delete_forum_reply(self, reply_id):
        """Deletes a reply and ALL nested descendants + their votes.
        Returns deleted reply dict (with '_deleted_images') or None."""
        reply = self.get_forum_reply(reply_id)
        if reply is None:
            return None
        ids, images = self._collect_reply_subtree(reply_id)
        with self._lock:
            with self.backend.transaction():
                marks = ",".join("?" for _ in ids)
                self.backend.execute(
                    f"DELETE FROM forum_votes WHERE target_type = 'reply'"
                    f" AND target_id IN ({marks})",
                    ids,
                )
                self.backend.execute(
                    f"DELETE FROM forum_replies WHERE id IN ({marks})",
                    ids,
                )
        reply["_deleted_images"] = sorted(images)
        return reply

    def delete_forum_thread(self, thread_id):
        """Deletes thread + all replies + all votes.
        Returns list of image filenames."""
        with self._lock:
            rows = self.backend.query(
                "SELECT image FROM forum_threads WHERE id = ?"
                " UNION ALL"
                " SELECT image FROM forum_replies WHERE thread_id = ?",
                (thread_id, thread_id),
            )
            reply_rows = self.backend.query(
                "SELECT id FROM forum_replies WHERE thread_id = ?",
                (thread_id,),
            )
            with self.backend.transaction():
                if reply_rows:
                    marks = ",".join("?" for _ in reply_rows)
                    self.backend.execute(
                        f"DELETE FROM forum_votes WHERE target_type = 'reply'"
                        f" AND target_id IN ({marks})",
                        [r["id"] for r in reply_rows],
                    )
                self.backend.execute(
                    "DELETE FROM forum_votes WHERE target_type = 'thread'"
                    " AND target_id = ?",
                    (thread_id,),
                )
                # Replies cascade via FK; done explicitly for safety on
                # setups where foreign keys were toggled off.
                self.backend.execute(
                    "DELETE FROM forum_replies WHERE thread_id = ?",
                    (thread_id,),
                )
                self.backend.execute(
                    "DELETE FROM forum_threads WHERE id = ?", (thread_id,)
                )
        return [r["image"] for r in rows if r["image"]]

    # ------------------------------------------------------------------
    # FORUM: votes (likes / dislikes)
    # ------------------------------------------------------------------

    def set_vote(self, target_type, target_id, username, value):
        """
        value: 1 like, -1 dislike, 0 removes the vote (toggle off).
        Returns {'likes', 'dislikes', 'score', 'my_vote'}.
        """
        if target_type not in ("thread", "reply"):
            raise ValueError("Invalid vote target type")
        if value not in (-1, 0, 1):
            raise ValueError("Invalid vote value")
        with self._lock:
            if value == 0:
                self.backend.execute(
                    "DELETE FROM forum_votes WHERE target_type = ?"
                    " AND target_id = ? AND username = ?",
                    (target_type, target_id, username),
                )
            else:
                self.backend.execute(
                    "INSERT INTO forum_votes"
                    " (target_type, target_id, username, value, timestamp)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT (target_type, target_id, username)"
                    " DO UPDATE SET value = excluded.value,"
                    " timestamp = excluded.timestamp",
                    (target_type, target_id, username, value,
                     int(time.time() * 1000)),
                )
        return self.vote_summary(target_type, target_id, username)

    def vote_summary(self, target_type, target_id, viewer=None):
        with self._lock:
            row = self.backend.query_one(
                "SELECT"
                " COALESCE(SUM(value), 0) AS score,"
                " COALESCE(SUM(CASE WHEN value = 1 THEN 1 ELSE 0 END), 0)"
                "   AS likes,"
                " COALESCE(SUM(CASE WHEN value = -1 THEN 1 ELSE 0 END), 0)"
                "   AS dislikes"
                " FROM forum_votes WHERE target_type = ? AND target_id = ?",
                (target_type, target_id),
            )
        summary = {"likes": 0, "dislikes": 0, "score": 0, "my_vote": 0}
        if row:
            summary.update({
                "likes": int(row["likes"]),
                "dislikes": int(row["dislikes"]),
                "score": int(row["score"]),
            })
        mine = self._my_votes_map(target_type, [target_id], viewer)
        summary["my_vote"] = mine.get(target_id, 0)
        return summary

    # ------------------------------------------------------------------
    # FRIENDS
    #
    # Requests live at their natural key (requester, addressee); routes
    # address them as '<requester>|<addressee>'.
    # ------------------------------------------------------------------

    @staticmethod
    def _friend_row(row):
        return {
            "requester": row["requester"],
            "addressee": row["addressee"],
            "status": row["status"],
            "timestamp": row["timestamp"],
        }

    def friendship_status(self, a, b):
        """'self' | 'none' | 'friends' | 'pending_out' | 'pending_in'."""
        if a == b:
            return "self"
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM friendships"
                " WHERE (requester = ? AND addressee = ?)"
                " OR (requester = ? AND addressee = ?)",
                (a, b, b, a),
            )
        if row is None:
            return "none"
        if row["status"] == "accepted":
            return "friends"
        return "pending_out" if row["requester"] == a else "pending_in"

    def send_friend_request(self, requester, addressee):
        """Returns 'pending_out' or 'friends' (mutual add auto-accepts);
        raises ValueError on invalid state."""
        status = self.friendship_status(requester, addressee)
        if addressee == requester:
            raise ValueError("You cannot befriend yourself.")
        if status == "friends":
            raise ValueError("You are already friends.")
        if status == "pending_out":
            raise ValueError("Request already sent.")
        now = int(time.time() * 1000)
        with self._lock:
            if status == "pending_in":
                self.backend.execute(
                    "UPDATE friendships SET status = 'accepted',"
                    " timestamp = ? WHERE requester = ? AND addressee = ?",
                    (now, addressee, requester),
                )
                return "friends"
            self.backend.execute(
                "INSERT INTO friendships (requester, addressee, status,"
                " timestamp) VALUES (?, ?, 'pending', ?)",
                (requester, addressee, now),
            )
        return "pending_out"

    def get_friend_request(self, req_ref):
        """req_ref: '<requester>|<addressee>' (or a legacy numeric id,
        which can no longer be resolved and yields None)."""
        if req_ref is None:
            return None
        parts = str(req_ref).split("|", 1)
        if len(parts) != 2:
            return None
        with self._lock:
            row = self.backend.query_one(
                "SELECT * FROM friendships WHERE requester = ?"
                " AND addressee = ?",
                (parts[0], parts[1]),
            )
        return None if row is None else self._friend_row(row)

    def accept_friend_request(self, requester, addressee):
        with self._lock:
            self.backend.execute(
                "UPDATE friendships SET status = 'accepted', timestamp = ?"
                " WHERE requester = ? AND addressee = ?"
                " AND status = 'pending'",
                (int(time.time() * 1000), requester, addressee),
            )

    def decline_friend_request(self, requester, addressee):
        with self._lock:
            self.backend.execute(
                "DELETE FROM friendships WHERE requester = ?"
                " AND addressee = ?",
                (requester, addressee),
            )

    def list_friends(self, username):
        with self._lock:
            rows = self.backend.query(
                "SELECT CASE WHEN requester = ? THEN addressee"
                " ELSE requester END AS friend FROM friendships"
                " WHERE status = 'accepted'"
                " AND (requester = ? OR addressee = ?)"
                " ORDER BY LOWER(friend)",
                (username, username, username),
            )
        return [r["friend"] for r in rows]

    def list_pending_incoming(self, username):
        with self._lock:
            rows = self.backend.query(
                "SELECT * FROM friendships WHERE addressee = ?"
                " AND status = 'pending' ORDER BY timestamp DESC",
                (username,),
            )
        return [self._friend_row(r) for r in rows]

    def list_pending_outgoing(self, username):
        with self._lock:
            rows = self.backend.query(
                "SELECT * FROM friendships WHERE requester = ?"
                " AND status = 'pending' ORDER BY timestamp DESC",
                (username,),
            )
        return [self._friend_row(r) for r in rows]

    def remove_friend(self, a, b):
        with self._lock:
            self.backend.execute(
                "DELETE FROM friendships WHERE status = 'accepted'"
                " AND ((requester = ? AND addressee = ?)"
                " OR (requester = ? AND addressee = ?))",
                (a, b, b, a),
            )

    def delete_user_friendships(self, username):
        with self._lock:
            self.backend.execute(
                "DELETE FROM friendships WHERE requester = ?"
                " OR addressee = ?",
                (username, username),
            )

    # ------------------------------------------------------------------
    # BANS
    # ------------------------------------------------------------------

    def ban_user(self, username, reason=""):
        with self._lock:
            row = self.backend.query_one(
                "SELECT 1 AS ok FROM bans WHERE username = ?", (username,)
            )
            if row:
                self.backend.execute(
                    "UPDATE bans SET reason = ?, timestamp = ?"
                    " WHERE username = ?",
                    (reason, int(time.time() * 1000), username),
                )
            else:
                self.backend.execute(
                    "INSERT INTO bans (username, reason, timestamp)"
                    " VALUES (?, ?, ?)",
                    (username, reason, int(time.time() * 1000)),
                )

    def unban_user(self, username):
        with self._lock:
            self.backend.execute(
                "DELETE FROM bans WHERE username = ?", (username,)
            )

    def is_banned(self, username):
        with self._lock:
            row = self.backend.query_one(
                "SELECT 1 AS ok FROM bans WHERE username = ?", (username,)
            )
        return row is not None

    def all_bans(self):
        with self._lock:
            rows = self.backend.query("SELECT username FROM bans")
        return {r["username"] for r in rows}

    # ------------------------------------------------------------------
    # NOTIFICATIONS
    # ------------------------------------------------------------------

    def add_notification(self, username, ntype, actor, text, link):
        """Inserts one notification; returns its monotonically increasing
        integer id (portable across SQLite and PostgreSQL)."""
        with self._lock:
            with self.backend.transaction():
                self.backend.execute(
                    "UPDATE notification_seq SET seq = seq + 1"
                    " WHERE name = 'notifications'"
                )
                nid = self.backend.query_one(
                    "SELECT seq FROM notification_seq"
                    " WHERE name = 'notifications'"
                )["seq"]
                self.backend.execute(
                    "INSERT INTO notifications (nid, username, ntype, actor,"
                    " text, link, read, timestamp)"
                    " VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (nid, username, ntype, actor, text, link,
                     int(time.time() * 1000)),
                )
            return nid

    def get_notification(self, notif_id):
        with self._lock:
            return self.backend.query_one(
                "SELECT * FROM notifications WHERE nid = ?", (notif_id,)
            )

    def list_notifications(self, username, limit=30):
        with self._lock:
            return self.backend.query(
                "SELECT * FROM notifications WHERE username = ?"
                " ORDER BY timestamp DESC, nid DESC LIMIT ?",
                (username, limit),
            )

    def unread_notification_count(self, username):
        with self._lock:
            row = self.backend.query_one(
                "SELECT COUNT(*) AS n FROM notifications"
                " WHERE username = ? AND read = 0",
                (username,),
            )
        return row["n"]

    def mark_notifications_read(self, username, notif_id=None, link=None):
        """Mark all / one / everything matching a link-prefix as read."""
        with self._lock:
            if notif_id is not None:
                self.backend.execute(
                    "UPDATE notifications SET read = 1"
                    " WHERE username = ? AND nid = ?",
                    (username, notif_id),
                )
            elif link is not None:
                self.backend.execute(
                    "UPDATE notifications SET read = 1"
                    " WHERE username = ? AND read = 0 AND link LIKE ?",
                    (username, link + "%"),
                )
            else:
                self.backend.execute(
                    "UPDATE notifications SET read = 1"
                    " WHERE username = ? AND read = 0",
                    (username,),
                )

    def delete_user_notifications(self, username):
        with self._lock:
            self.backend.execute(
                "DELETE FROM notifications WHERE username = ?", (username,)
            )

    # ------------------------------------------------------------------
    # ACCOUNT ADMIN
    # ------------------------------------------------------------------

    def change_password(self, username, password_hash, expect_id=None):
        with self._lock:
            if expect_id is not None:
                self.backend.execute(
                    "UPDATE users SET password_hash = ?"
                    " WHERE username = ? AND id = ?",
                    (password_hash, username, expect_id),
                )
            else:
                self.backend.execute(
                    "UPDATE users SET password_hash = ? WHERE username = ?",
                    (password_hash, username),
                )

    def delete_user_account(self, username, expect_id=None):
        """
        Removes the account plus everything owned by it EXCEPT chat
        messages (they stay visible like on Telegram).

        When `expect_id` is provided the deletion only proceeds if the
        stored account id matches - preventing deletion of a NEW account
        that merely reused the username.
        """
        with self._lock:
            row = self.backend.query_one(
                "SELECT id, avatar FROM users WHERE username = ?",
                (username,),
            )
            if row is None:
                return None
            if expect_id is not None and row["id"] != expect_id:
                log.warning(
                    "Refusing to delete '%s': supplied id does not match "
                    "the account id.", username
                )
                return None
            avatar = row["avatar"]
            uid = row["id"]

            with self.backend.transaction():
                # Groups they created are removed entirely (incl. msgs).
                created_group_ids = [
                    r["id"]
                    for r in self.backend.query(
                        "SELECT id FROM groups"
                        " WHERE creator = ? OR creator_id = ?",
                        (username, uid),
                    )
                ]
                group_files = []
                for gid in created_group_ids:
                    group_files.extend(
                        self.delete_chat_messages("group", gid)
                    )
                    self.backend.execute(
                        "DELETE FROM group_members WHERE group_id = ?",
                        (gid,),
                    )
                self.backend.execute(
                    "DELETE FROM groups WHERE creator = ? OR creator_id = ?",
                    (username, uid),
                )

                # Forum posts owned by the user (by id OR legacy name).
                thread_rows = self.backend.query(
                    "SELECT id FROM forum_threads"
                    " WHERE author = ? OR author_id = ?",
                    (username, uid),
                )
                forum_files = []
                for t in thread_rows:
                    forum_files.extend(self.delete_forum_thread(t["id"]))
                orphan_rows = self.backend.query(
                    "SELECT id, image FROM forum_replies"
                    " WHERE author = ? OR author_id = ?",
                    (username, uid),
                )
                orphan_ids = [r["id"] for r in orphan_rows]
                if orphan_ids:
                    marks = ",".join("?" for _ in orphan_ids)
                    self.backend.execute(
                        f"DELETE FROM forum_votes WHERE target_type = 'reply'"
                        f" AND target_id IN ({marks})",
                        orphan_ids,
                    )
                    self.backend.execute(
                        f"DELETE FROM forum_replies WHERE id IN ({marks})",
                        orphan_ids,
                    )
                forum_files.extend(
                    r["image"] for r in orphan_rows if r["image"]
                )

                self.backend.execute(
                    "DELETE FROM forum_votes WHERE username = ?",
                    (username,),
                )
                self.delete_user_friendships(username)
                self.delete_user_notifications(username)

                self.backend.execute(
                    "DELETE FROM group_members WHERE username = ?",
                    (username,),
                )
                self.backend.execute(
                    "DELETE FROM bans WHERE username = ?", (username,)
                )
                self.backend.execute(
                    "DELETE FROM users WHERE username = ? AND id = ?",
                    (username, uid),
                )

        return {
            "id": uid,
            "avatar": avatar,
            "forum_images": forum_files,
            "group_images": group_files,
            "deleted_group_ids": created_group_ids,
        }

    # ------------------------------------------------------------------
    # LEGACY JSON MIGRATION (fresh databases only)
    # ------------------------------------------------------------------

    def migrate_legacy_if_empty(self, base_dir):
        """
        One-time import from the old JSON storage. Runs only when the
        users table is empty. Raises RuntimeError if encrypted
        conversations cannot be decrypted with the current key.
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
                        f"Cannot decrypt legacy conversation '{path}' with "
                        "the current DM_ENCRYPTION_KEY. Restore the key "
                        "that was used when the file was written, or move "
                        "the file away to skip importing it."
                    )
                return decoded if isinstance(decoded, list) else []
            return data if isinstance(data, list) else []

        def norm_message(msg):
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
            with self.backend.transaction():
                user_ids = {}

                users = load_json(os.path.join(base_dir, "users.json"), {})
                if isinstance(users, dict):
                    for username, data in users.items():
                        if not isinstance(data, dict):
                            continue
                        uid = _new_id()
                        user_ids[username] = uid
                        self.backend.execute(
                            self.backend.or_ignore(
                                "INSERT INTO users (id, username,"
                                " password_hash, display_name, bio, avatar)"
                                " VALUES (?, ?, ?, ?, ?, ?)"
                            ),
                            (
                                uid, username, data.get("password", ""),
                                data.get("display_name", "") or "",
                                data.get("bio", "") or "",
                                data.get("avatar"),
                            ),
                        )
                        stats["users"] += 1

                groups = load_json(os.path.join(base_dir, "groups.json"), {})
                if isinstance(groups, dict):
                    for gid, group in groups.items():
                        if not isinstance(group, dict):
                            continue
                        creator = group.get("creator", "")
                        self.backend.execute(
                            self.backend.or_ignore(
                                "INSERT INTO groups (id, name, creator,"
                                " creator_id, password_hash)"
                                " VALUES (?, ?, ?, ?, ?)"
                            ),
                            (
                                gid, group.get("name", "Unnamed group"),
                                creator, user_ids.get(creator),
                                group.get("password_hash", ""),
                            ),
                        )
                        stats["groups"] += 1
                        for member in group.get("members", []):
                            self.backend.execute(
                                self.backend.or_ignore(
                                    "INSERT INTO group_members"
                                    " (group_id, username) VALUES (?, ?)"
                                ),
                                (gid, member),
                            )
                            stats["memberships"] += 1

                def insert_msg(m, chat_type, chat_id, encrypt_text):
                    self.backend.execute(
                        self.backend.or_ignore(
                            "INSERT INTO messages (id, chat_type, chat_id,"
                            " sender, sender_id, text, file, reply, timestamp)"
                            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
                        ),
                        (
                            m["id"], chat_type, chat_id, m["from"],
                            user_ids.get(m["from"]),
                            encrypt_text(m["text"]),
                            m["file"],
                            self.encrypt_field(m["reply"])
                            if m["reply"] else None,
                            m["timestamp"],
                        ),
                    )

                global_msgs = load_json(
                    os.path.join(base_dir, "messages.json"), []
                )
                if isinstance(global_msgs, list):
                    for msg in global_msgs:
                        if not isinstance(msg, dict):
                            continue
                        insert_msg(norm_message(msg), "global", "",
                                   lambda t: t)
                        stats["global_messages"] += 1

                for filepath in sorted(
                    _glob.glob(os.path.join(dm_dir, "dm_*.json"))
                ):
                    chat_id = os.path.basename(filepath)[3:-5]
                    count = 0
                    for msg in load_encrypted_or_plain(filepath):
                        if not isinstance(msg, dict):
                            continue
                        insert_msg(norm_message(msg), "dm", chat_id,
                                   self.encrypt_field)
                        count += 1
                    stats["dm_conversations"] += 1
                    stats["dm_messages"] += count

                for filepath in sorted(
                    _glob.glob(os.path.join(gm_dir, "group_*.json"))
                ):
                    chat_id = os.path.basename(filepath)[6:-5]
                    count = 0
                    for msg in load_encrypted_or_plain(filepath):
                        if not isinstance(msg, dict):
                            continue
                        insert_msg(norm_message(msg), "group", chat_id,
                                   self.encrypt_field)
                        count += 1
                    stats["group_message_files"] += 1
                    stats["group_messages"] += count

        return stats
