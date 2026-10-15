"""Sync agent: link monitor, uploader (live + catch-up) and offline processor.

Runs as its own process so the collector never waits on the network.

Mode rules (each switch well inside 10 s):
  online  -> offline after a failed upload (hard deadline 4 s) confirmed by a failed
            health check (2.5 s): worst case about 7 s
  offline -> online  after 2 consecutive successful health checks (every 2 s)

Exactly-once to the cloud: rows are marked uploaded only after the cloud
acknowledges them; the cloud inserts with ON CONFLICT DO NOTHING, so a retry
after a lost acknowledgement cannot create a duplicate.
"""
import json
import sys
import time

from . import config, processing as P
from .cloud import Cloud, CloudError
from .store import Store

LOOP = 0.5
FAILS_TO_OFFLINE = 2
OKS_TO_ONLINE = 2
MAX_SAMPLES_PER_SENSOR = 12_000     # 30 s of data per request during catch-up
OFFLINE_FALLBACK_S = 4.0            # online but unacknowledged this long -> device processes it


def log(*a):
    print(time.strftime("%H:%M:%S"), "[sync]", *a, file=sys.stderr, flush=True)


def acked_through(store, sid, sensor):
    """Every index below this is in the cloud (or recorded lost there)."""
    a = store.db.execute("SELECT MIN(first_index) FROM chunks WHERE uploaded=0 AND session_id=? AND sensor=?",
                         (sid, sensor)).fetchone()[0]
    b = store.db.execute("SELECT MIN(from_index) FROM losses WHERE uploaded=0 AND session_id=? AND sensor=?",
                         (sid, sensor)).fetchone()[0]
    cands = [x for x in (a, b) if x is not None]
    return min(cands) if cands else store.progress(sid)[sensor]["next"]


def status(store):
    sid = store.latest_session()
    saved = store.samples_saved()
    cloud = store.samples_uploaded()
    mode = store.get_kv("mode", {"mode": "unknown", "since": None})
    return {"mode": mode["mode"], "mode_since": mode.get("since"), "session": sid,
            "samples_saved": saved, "samples_in_cloud": cloud, "backlog": saved - cloud,
            "losses": store.db.execute("SELECT COUNT(*) FROM losses").fetchone()[0],
            "offline_results": store.db.execute("SELECT COUNT(*) FROM results WHERE mode='offline'").fetchone()[0]}


class Sync:
    def __init__(self, store, cloud):
        self.store = store
        self.cloud = cloud
        m = store.get_kv("mode", {"mode": "offline"})
        self.mode = m["mode"] if m["mode"] in ("online", "offline") else "offline"
        self.fails = 0
        self.oks = 0
        self.ready_since = {}
        self.last_health = 0.0
        self.net_bytes = 0

    # ---- mode --------------------------------------------------------------
    def set_mode(self, mode):
        if mode != self.mode:
            log(f"mode {self.mode} -> {mode}")
            self.mode = mode
            with self.store.tx():
                self.store.set_kv("mode", {"mode": mode, "since": time.time()})

    def record(self, ok):
        if ok:
            self.fails = 0
            self.oks += 1
            if self.mode == "offline" and self.oks >= OKS_TO_ONLINE:
                self.set_mode("online")
        else:
            self.oks = 0
            self.fails += 1
            if self.mode == "online" and self.fails >= FAILS_TO_OFFLINE:
                self.set_mode("offline")

    # ---- upload --------------------------------------------------------------
    def pending_session(self):
        row = self.store.db.execute(
            "SELECT s.session_id FROM sessions s WHERE EXISTS (SELECT 1 FROM chunks c WHERE c.session_id=s.session_id AND c.uploaded=0)"
            " OR EXISTS (SELECT 1 FROM losses l WHERE l.session_id=s.session_id AND l.uploaded=0)"
            " ORDER BY s.seq LIMIT 1").fetchone()
        if row:
            return row[0]
        # clean session ends not yet announced to the cloud
        for sid in self.store.sessions():
            for s, p in self.store.progress(sid).items():
                if p["ended"] and not self.store.get_kv(f"end_sent:{sid}:{s}") and not self._ended_by_loss(sid, s):
                    return sid
        return None

    def _ended_by_loss(self, sid, s):
        return self.store.db.execute("SELECT 1 FROM losses WHERE session_id=? AND sensor=? AND to_index IS NULL",
                                     (sid, s)).fetchone() is not None

    def build_batch(self, sid):
        items, marks = [], {"chunks": [], "losses": [], "ends": []}
        for s in (1, 2):
            rows = self.store.db.execute(
                "SELECT first_index,count,data,rx_time FROM chunks WHERE uploaded=0 AND session_id=? AND sensor=? "
                "ORDER BY first_index", (sid, s))
            total, cutoff = 0, None
            for first, count, data, rx in rows:
                if total >= MAX_SAMPLES_PER_SENSOR:
                    cutoff = first
                    break
                items.append({"type": "chunk", "sensor": s, "first_index": first,
                              "data": json.loads(data), "rx_time": rx})
                marks["chunks"].append((s, first))
                total += count
            for frm, to, reason in self.store.db.execute(
                    "SELECT from_index,to_index,reason FROM losses WHERE uploaded=0 AND session_id=? AND sensor=? "
                    "ORDER BY from_index", (sid, s)):
                if cutoff is not None and frm >= cutoff:
                    break
                items.append({"type": "loss", "sensor": s, "from_index": frm, "to_index": to, "reason": reason})
                marks["losses"].append((s, frm))
            p = self.store.progress(sid)[s]
            if cutoff is None and p["ended"] and not self._ended_by_loss(sid, s) \
                    and not self.store.get_kv(f"end_sent:{sid}:{s}"):
                items.append({"type": "end", "sensor": s, "at": p["next"]})
                marks["ends"].append(s)
        return items, marks

    def upload_once(self):
        sid = self.pending_session()
        if sid is None:
            return False
        items, marks = self.build_batch(sid)
        if not items:
            return False
        t0 = time.monotonic()
        _, nbytes = self.cloud.ingest(sid, items, timeout=4.0)
        dt = time.monotonic() - t0
        if dt > 2:
            log(f"slow ingest: {dt:.1f}s for {len(items)} items")
        self.net_bytes += nbytes
        with self.store.tx() as db:
            db.executemany("UPDATE chunks SET uploaded=1 WHERE session_id=? AND sensor=? AND first_index=?",
                           [(sid, s, f) for s, f in marks["chunks"]])
            db.executemany("UPDATE losses SET uploaded=1 WHERE session_id=? AND sensor=? AND from_index=?",
                           [(sid, s, f) for s, f in marks["losses"]])
            for s in marks["ends"]:
                self.store.set_kv(f"end_sent:{sid}:{s}", True)
        return True

    # ---- offline processing ----------------------------------------------------
    def process(self):
        for sid in self.store.sessions():
            key = f"proc:{sid}"
            state = self.store.get_kv(key) or P.new_state()
            if state.get("done"):
                continue
            prog = self.store.progress(sid)
            known = {s: prog[s]["next"] for s in (1, 2)}
            ended = {s: prog[s]["ended"] for s in (1, 2)}
            changed = False
            new_results = []
            while P.ready(state["next_window"], known, ended):
                w = state["next_window"]
                need = {1: P.W * (w + 1) + 1, 2: P.W * (w + 1)}
                in_cloud = all(acked_through(self.store, sid, s) >= need[s] or
                               (ended[s] and acked_through(self.store, sid, s) >= known[s]) for s in (1, 2))
                t = self.ready_since.setdefault((sid, w), time.monotonic())
                store_it = self.mode == "offline" or (not in_cloud and time.monotonic() - t > OFFLINE_FALLBACK_S)
                if not store_it and not in_cloud:
                    break  # online and on its way to the cloud: give the cloud time to process it
                res = P.process_window(state, w, lambda s, lo, hi: self.store.load_range(sid, s, lo, hi))
                self.ready_since.pop((sid, w), None)
                changed = True
                if store_it:
                    new_results.append((sid, w, "offline", json.dumps(
                        {"session": sid, "window": w, "mode": "offline", **res, "processed_at": time.time()})))
            if ended[1] and ended[2] and not P.ready(state["next_window"], known, ended):
                state["done"] = True
                changed = True
            if changed:
                with self.store.tx() as db:
                    db.executemany("INSERT OR IGNORE INTO results VALUES(?,?,?,?)", new_results)
                    self.store.set_kv(key, state)

    # ---- loop -----------------------------------------------------------------
    def run(self):
        log("starting in mode", self.mode)
        while True:
            t0 = time.monotonic()
            try:
                if self.mode == "online" and self.fails:
                    # one call already failed: confirm quickly before sending more data
                    try:
                        self.cloud.health(timeout=2.5)
                        self.record(True)
                    except CloudError as e:
                        log("health check failed:", e)
                        self.record(False)
                elif self.mode == "online":
                    try:
                        sent = self.upload_once()
                        if not sent and t0 - self.last_health > 2:
                            self.cloud.health(timeout=3)
                            self.last_health = t0
                        self.record(True)
                        while sent and self.mode == "online" and time.monotonic() - t0 < LOOP:
                            sent = self.upload_once()  # catch-up: keep going while it is fast
                    except CloudError as e:
                        log("cloud call failed:", e)
                        self.record(False)
                elif t0 - self.last_health >= 2:
                    self.last_health = t0
                    try:
                        self.cloud.health(timeout=2)
                        self.record(True)
                    except CloudError:
                        self.record(False)
                self.process()
            except Exception as e:  # keep the agent alive; the collector is unaffected
                log("error:", repr(e))
            time.sleep(max(0.0, LOOP - (time.monotonic() - t0)))


def main(db_path):
    Sync(Store(db_path), Cloud()).run()
