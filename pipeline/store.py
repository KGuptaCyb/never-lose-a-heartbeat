"""Durable device storage (SQLite, WAL, synchronous=FULL).

Invariant per (session, sensor): chunks and losses tile [0, next_index)
exactly once, with no overlap. Every commit is fsynced before the collector
asks the mock for more data, so a power cut loses at most the reply in flight.
"""
import json
import os
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  seq        INTEGER NOT NULL UNIQUE,
  first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS progress (
  session_id TEXT NOT NULL,
  sensor     INTEGER NOT NULL,
  next_index INTEGER NOT NULL,
  ended      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, sensor)
);
CREATE TABLE IF NOT EXISTS chunks (
  session_id  TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  first_index INTEGER NOT NULL,
  count       INTEGER NOT NULL,
  data        TEXT NOT NULL,
  rx_time     REAL NOT NULL,
  uploaded    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, sensor, first_index)
);
CREATE INDEX IF NOT EXISTS chunks_pending ON chunks(uploaded, session_id, sensor, first_index);
CREATE TABLE IF NOT EXISTS losses (
  session_id  TEXT NOT NULL,
  sensor      INTEGER NOT NULL,
  from_index  INTEGER NOT NULL,
  to_index    INTEGER,
  reason      TEXT NOT NULL,
  uploaded    INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, sensor, from_index)
);
CREATE TABLE IF NOT EXISTS results (
  session_id TEXT NOT NULL,
  window     INTEGER NOT NULL,
  mode       TEXT NOT NULL,
  payload    TEXT NOT NULL,
  PRIMARY KEY (session_id, window, mode)
);
CREATE TABLE IF NOT EXISTS kv (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""


def connect(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    db = sqlite3.connect(path, timeout=30, isolation_level=None)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=FULL")
    db.execute("PRAGMA busy_timeout=30000")
    db.executescript(SCHEMA)
    return db


class Store:
    def __init__(self, path):
        self.path = path
        self.db = connect(path)

    # ---- generic helpers -------------------------------------------------
    def get_kv(self, key, default=None):
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_kv(self, key, value):
        self.db.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, json.dumps(value)))

    def tx(self):
        return _Tx(self.db)

    # ---- sessions / progress ---------------------------------------------
    def latest_session(self):
        row = self.db.execute("SELECT session_id FROM sessions ORDER BY seq DESC LIMIT 1").fetchone()
        return row[0] if row else None

    def sessions(self):
        return [r[0] for r in self.db.execute("SELECT session_id FROM sessions ORDER BY seq")]

    def add_session(self, sid):
        seq = self.db.execute("SELECT COALESCE(MAX(seq),0)+1 FROM sessions").fetchone()[0]
        self.db.execute("INSERT OR IGNORE INTO sessions VALUES(?,?,?)", (sid, seq, time.time()))
        for s in (1, 2):
            self.db.execute("INSERT OR IGNORE INTO progress(session_id,sensor,next_index) VALUES(?,?,0)", (sid, s))

    def progress(self, sid):
        out = {}
        for sensor, nxt, ended in self.db.execute(
                "SELECT sensor,next_index,ended FROM progress WHERE session_id=?", (sid,)):
            out[sensor] = {"next": nxt, "ended": bool(ended)}
        return out

    # ---- writes (call inside a transaction) -------------------------------
    def add_chunk(self, sid, sensor, first, rows):
        # rx_time is informational only (used to report latency); no logic depends on the wall clock
        self.db.execute("INSERT INTO chunks(session_id,sensor,first_index,count,data,rx_time) VALUES(?,?,?,?,?,?)",
                        (sid, sensor, first, len(rows), json.dumps(rows, separators=(",", ":")), time.time()))
        self.db.execute("UPDATE progress SET next_index=? WHERE session_id=? AND sensor=?",
                        (first + len(rows), sid, sensor))

    def add_loss(self, sid, sensor, frm, to, reason):
        self.db.execute("INSERT INTO losses(session_id,sensor,from_index,to_index,reason) VALUES(?,?,?,?,?)",
                        (sid, sensor, frm, to, reason))
        if to is None:
            self.db.execute("UPDATE progress SET ended=1 WHERE session_id=? AND sensor=?", (sid, sensor))
        else:
            self.db.execute("UPDATE progress SET next_index=? WHERE session_id=? AND sensor=?",
                            (to + 1, sid, sensor))

    def mark_ended(self, sid, sensor):
        self.db.execute("UPDATE progress SET ended=1 WHERE session_id=? AND sensor=?", (sid, sensor))

    # ---- reads ------------------------------------------------------------
    def iter_records(self, sid, sensor):
        """Chunks and losses for one sensor, in index order."""
        chunks = self.db.execute(
            "SELECT first_index,count,data FROM chunks WHERE session_id=? AND sensor=? ORDER BY first_index",
            (sid, sensor)).fetchall()
        losses = self.db.execute(
            "SELECT from_index,to_index,reason FROM losses WHERE session_id=? AND sensor=? ORDER BY from_index",
            (sid, sensor)).fetchall()
        return chunks, losses

    def samples_saved(self, sid=None):
        q = "SELECT COALESCE(SUM(count),0) FROM chunks"
        return self.db.execute(q + (" WHERE session_id=?" if sid else ""), (sid,) if sid else ()).fetchone()[0]

    def samples_uploaded(self, sid=None):
        q = "SELECT COALESCE(SUM(count),0) FROM chunks WHERE uploaded=1"
        return self.db.execute(q + (" AND session_id=?" if sid else ""), (sid,) if sid else ()).fetchone()[0]

    def load_range(self, sid, sensor, lo, hi):
        """Samples with lo <= index < hi as {index: values}."""
        out = {}
        for first, count, data in self.db.execute(
                "SELECT first_index,count,data FROM chunks WHERE session_id=? AND sensor=? "
                "AND first_index < ? AND first_index + count > ? ORDER BY first_index",
                (sid, sensor, hi, lo)):
            rows = json.loads(data)
            for k, v in enumerate(rows):
                i = first + k
                if lo <= i < hi:
                    out[i] = v
        return out


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None:
            self.db.execute("COMMIT")
        else:
            self.db.execute("ROLLBACK")
        return False
