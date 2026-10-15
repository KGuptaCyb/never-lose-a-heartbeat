"""Reads both sensors from the mock and saves every sample durably.

The collector never touches the network beyond the local mock and never
waits on the cloud. It always asks for data with an explicit from_index equal
to the next index it still needs, so after a crash, a corrupt reply, a
duplicate or a timeout it simply asks again for exactly what is missing.
"""
import json
import os
import select
import socket
import sqlite3
import sys
import time

from .protocol import CorruptReply, parse_reply
from .store import Store

POLL_INTERVAL = 0.25      # seconds between read cycles (per sensor)
REPLY_TIMEOUT = 3.0       # mock "stops answering"
GAP_CONFIRMATIONS = 2     # unexplained gap must be seen this many times before it is a loss
RESERVE_BYTES = 32 * 1024 * 1024


def log(*a):
    print(time.strftime("%H:%M:%S"), "[collector]", *a, file=sys.stderr, flush=True)


class LineConn:
    def __init__(self, host, port):
        self.sock = socket.create_connection((host, port), timeout=REPLY_TIMEOUT)
        self.sock.setblocking(False)
        self.buf = b""

    def send(self, obj):
        self.sock.setblocking(True)
        self.sock.settimeout(REPLY_TIMEOUT)
        self.sock.sendall((json.dumps(obj) + "\n").encode())
        self.sock.setblocking(False)

    def read_lines(self, timeout):
        """Wait up to `timeout` for at least one line, then drain what is ready."""
        deadline = time.monotonic() + timeout
        lines = []
        while True:
            while b"\n" in self.buf:
                line, self.buf = self.buf.split(b"\n", 1)
                if line.strip():
                    lines.append(line)
            remaining = deadline - time.monotonic()
            if lines:
                remaining = min(remaining, 0.02)  # drain duplicates that arrive right behind
            if remaining <= 0:
                return lines
            r, _, _ = select.select([self.sock], [], [], remaining)
            if not r:
                return lines
            chunk = self.sock.recv(1 << 20)
            if not chunk:
                raise ConnectionError("mock closed connection")
            self.buf += chunk

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


class Collector:
    def __init__(self, store, host="127.0.0.1", port=7100):
        self.store = store
        self.host, self.port = host, port
        self.conn = None
        self.sid = store.latest_session()
        self.gap_seen = {}           # sensor -> (next_index, times)
        self.disk_full_since = None  # monotonic time the disk filled
        self.reserve = os.path.join(os.path.dirname(os.path.abspath(store.path)), "reserve.bin")
        self.stats = {"replies": 0, "corrupt": 0, "dups": 0}

    # ---- disk reserve ------------------------------------------------------
    def ensure_reserve(self):
        if os.path.exists(self.reserve):
            return
        try:
            with open(self.reserve, "wb") as f:
                if hasattr(os, "posix_fallocate"):
                    os.posix_fallocate(f.fileno(), 0, RESERVE_BYTES)
                else:
                    f.truncate(RESERVE_BYTES)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            log("could not create disk reserve:", e)

    def release_reserve(self):
        if os.path.exists(self.reserve):
            os.remove(self.reserve)
            log("disk full: released reserve file to keep saving")
            return True
        return False

    # ---- main loop ---------------------------------------------------------
    def run(self, stop_after=None):
        self.ensure_reserve()
        t_end = time.monotonic() + stop_after if stop_after else None
        while t_end is None or time.monotonic() < t_end:
            cycle_start = time.monotonic()
            try:
                if self.conn is None:
                    self.conn = LineConn(self.host, self.port)
                    log("connected to mock")
                self.cycle()
            except (OSError, ConnectionError) as e:
                if self.conn is not None:
                    log("mock connection problem:", e)
                    self.conn.close()
                self.conn = None
                time.sleep(0.5)
                continue
            time.sleep(max(0.0, POLL_INTERVAL - (time.monotonic() - cycle_start)))

    def cycle(self):
        replies = []
        sensors = self.wanted_sensors()
        for sensor in sensors:
            req = {"cmd": "read", "sensor": sensor}
            if self.sid is not None:
                req["from_index"] = self.store.progress(self.sid)[sensor]["next"]
            self.conn.send(req)
            lines = self.conn.read_lines(REPLY_TIMEOUT)
            if not lines:
                raise ConnectionError("mock did not answer within %.0fs" % REPLY_TIMEOUT)
            for line in lines:
                try:
                    replies.append(parse_reply(line))
                except CorruptReply as e:
                    self.stats["corrupt"] += 1
                    log("discarding corrupt reply:", e)
        if replies:
            self.save(replies)

    def wanted_sensors(self):
        if self.sid is None:
            return [1, 2]
        prog = self.store.progress(self.sid)
        wanted = [s for s in (1, 2) if not prog[s]["ended"]]
        return wanted or [1, 2]  # both ended: keep polling to notice a new session

    # ---- saving --------------------------------------------------------------
    def save(self, replies):
        while True:
            try:
                with self.store.tx():
                    for msg in replies:
                        self.apply(msg)
                if self.disk_full_since is not None:
                    log("disk writable again")
                    self.disk_full_since = None
                return
            except sqlite3.OperationalError as e:
                if "full" not in str(e).lower():
                    raise
                if self.disk_full_since is None:
                    self.disk_full_since = time.monotonic()
                self.sid = self.store.latest_session()
                if self.release_reserve():
                    continue
                log("DISK FULL: cannot save; data will be re-requested and any overflow recorded as lost")
                return

    def apply(self, msg):
        sid = msg["session_id"]
        if sid != self.sid:
            known = self.store.db.execute("SELECT 1 FROM sessions WHERE session_id=?", (sid,)).fetchone()
            if known:
                self.stats["dups"] += 1
                return  # late reply from an older session; it was already closed
            self.start_session(sid)
        sensor = msg["sensor"]
        prog = self.store.progress(sid)[sensor]
        if prog["ended"]:
            return
        nxt = prog["next"]

        segs = []
        for l in msg.get("lost") or []:
            segs.append((l["from_index"], "loss", l.get("to_index"), self.reason(l.get("reason") or "lost_by_mock")))
        first, rows = msg["first_index"], msg["data"]
        if rows:
            segs.append((first, "data", rows, None))
        segs.sort(key=lambda s: s[0])

        for start, kind, payload, reason in segs:
            end = (start + len(payload) - 1) if kind == "data" else payload
            if end is not None and end < nxt:
                if kind == "data":
                    self.stats["dups"] += 1
                continue
            if start > nxt:
                key = (sensor, nxt)
                times = self.gap_seen.get(key, 0) + 1
                self.gap_seen[key] = times
                if times < GAP_CONFIRMATIONS:
                    return  # ask again from nxt before declaring anything lost
                self.store.add_loss(sid, sensor, nxt, start - 1, self.reason("not_delivered_by_mock"))
                log(f"loss s{sensor} [{nxt},{start - 1}] not delivered")
                nxt = start
            if kind == "loss":
                self.store.add_loss(sid, sensor, nxt, end, reason)
                log(f"loss s{sensor} [{nxt},{end}] {reason}")
                if end is None:
                    return
                nxt = end + 1
            else:
                part = payload[nxt - start:]
                self.store.add_chunk(sid, sensor, nxt, part)
                nxt = start + len(payload)
        self.gap_seen = {k: v for k, v in self.gap_seen.items() if k[0] != sensor}
        if msg.get("session_end") and nxt >= first + len(rows):
            self.store.mark_ended(sid, sensor)
            log(f"session {sid} sensor {sensor} ended at {nxt}")
        self.stats["replies"] += 1

    def reason(self, base):
        if self.disk_full_since is not None:
            return f"{base} (device disk was full)"
        return base

    def start_session(self, sid):
        old = self.sid
        if old is not None:
            for sensor, p in self.store.progress(old).items():
                if not p["ended"]:
                    self.store.add_loss(old, sensor, p["next"], None,
                                        "mock_restarted: unread data of old session not delivered")
                    log(f"session {old} s{sensor}: lost from {p['next']} (end unknown) - mock restarted")
        self.store.add_session(sid)
        self.sid = sid
        log("new session", sid)


def main(db_path, host, port):
    Collector(Store(db_path), host, port).run()
