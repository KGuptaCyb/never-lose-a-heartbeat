#!/usr/bin/env python3
"""Stand-in for biosignal-mock, written from the protocol in section 6 of the brief.

Used only for development until the real mock is available. It is NOT the
judged mock; always re-test against the real one and check_ground_truth.

Extra fault-injection flags (all off by default) imitate section 6.3:
  --corrupt-rate P   send a reply with a wrong crc32 with probability P
  --dup-rate P       send the same reply twice with probability P
  --pause-every S --pause-len L   stop answering for L seconds every S seconds
  --drop-sensor N --drop-at S --drop-len L   sensor N stops for L seconds at S
  --restart-at S     start a new session S seconds in (indexes restart at 0)
"""
import argparse
import json
import math
import random
import socket
import threading
import time
import uuid
import zlib

RATE = 400
BUFFER_SECONDS = 10
MOD = 8_388_608


def to_s24(v):
    v = int(v)
    return max(-(1 << 23), min((1 << 23) - 1, v))


class Signal:
    """Deterministic values for (sensor, index) given mode and seed."""

    def __init__(self, mode, seed):
        self.mode = mode
        self.seed = seed
        self._beats = []  # beat sample indexes, generated lazily
        self._next_beat_t = 0.5

    def _bpm(self, t):
        minute = (t / 60.0) % 6
        return 60 if minute < 2 else (90 if minute < 4 else 72)

    def _beats_until(self, idx):
        while not self._beats or self._beats[-1] < idx + RATE:
            t = self._next_beat_t
            self._beats.append(int(round(t * RATE)))
            self._next_beat_t = t + 60.0 / self._bpm(t)

    def ecg(self, i):
        self._beats_until(i)
        # nearest beat
        lo, hi = 0, len(self._beats) - 1
        while lo < hi:
            mid = (lo + hi) // 2
            if self._beats[mid] < i:
                lo = mid + 1
            else:
                hi = mid
        d = min(abs(i - self._beats[lo]), abs(i - self._beats[lo - 1]) if lo else 10**9)
        rnd = random.Random(self.seed * 1_000_003 + i)
        noise = rnd.randint(-2000, 2000)
        return 50_000 + noise + int(1_000_000 * math.exp(-(d * d) / 18.0))

    def sample(self, sensor, i):
        if self.mode == "ramp":
            if sensor == 1:
                return [(3 * i + c) % MOD for c in (1, 2, 3)]
            return [(2 * i + c) % MOD for c in (1, 2)]
        if sensor == 1:
            v = self.ecg(i)
            return [to_s24(v * 0.6), to_s24(v), to_s24(v * 1.3)]
        t = i / RATE
        return [to_s24(400_000 + 20_000 * math.sin(t / 30 + self.seed)),
                to_s24(150_000 + 10_000 * math.cos(t / 45 + self.seed))]


class Session:
    def __init__(self, args, start):
        self.id = uuid.uuid4().hex[:8]
        self.start = start
        self.duration = args.duration
        self.signal = Signal(args.mode, args.seed)
        self.cursor = {1: 0, 2: 0}

    def produced(self, now):
        return min(int((now - self.start) * RATE), self.duration * RATE)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["signal", "ramp"], default="signal")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--duration", type=int, default=3600)
    ap.add_argument("--port", type=int, default=7100)
    ap.add_argument("--corrupt-rate", type=float, default=0)
    ap.add_argument("--dup-rate", type=float, default=0)
    ap.add_argument("--pause-every", type=float, default=0)
    ap.add_argument("--pause-len", type=float, default=0)
    ap.add_argument("--drop-sensor", type=int, default=0)
    ap.add_argument("--drop-at", type=float, default=0)
    ap.add_argument("--drop-len", type=float, default=0)
    ap.add_argument("--restart-at", type=float, default=0)
    args = ap.parse_args()

    t0 = time.monotonic()
    state = {"session": Session(args, t0), "restarted": False}
    lock = threading.Lock()
    rnd = random.Random(args.seed)

    def current():
        now = time.monotonic()
        if args.restart_at and not state["restarted"] and now - t0 >= args.restart_at:
            state["session"] = Session(args, now)
            state["restarted"] = True
        return state["session"], now

    def dropped(sess, sensor, i):
        if sensor != args.drop_sensor or not args.drop_len:
            return False
        a = int(args.drop_at * RATE)
        return a <= i < a + int(args.drop_len * RATE)

    def handle(req):
        sess, now = current()
        sensor = int(req["sensor"])
        produced = sess.produced(now)
        oldest = max(0, produced - BUFFER_SECONDS * RATE)
        start = req.get("from_index", sess.cursor[sensor])
        lost = []
        if start < oldest:
            lost.append({"from_index": start, "to_index": oldest - 1, "reason": "buffer_overflow"})
            start = oldest
        end = min(produced, start + RATE)
        rows = []
        first = start
        for i in range(start, end):
            if dropped(sess, sensor, i):
                if rows:  # stop at the drop, deliver the rest next time
                    end = i
                    break
                a = int(args.drop_at * RATE)
                b = a + int(args.drop_len * RATE) - 1
                lost.append({"from_index": i, "to_index": b, "reason": "sensor_dropout"})
                first = b + 1
                end = first
                break
            rows.append(sess.signal.sample(sensor, i))
        if not rows:
            first = max(first, end)
        sess.cursor[sensor] = first + len(rows)
        data_text = json.dumps(rows)
        crc = format(zlib.crc32(data_text.encode()) & 0xFFFFFFFF, "08x")
        if args.corrupt_rate and rnd.random() < args.corrupt_rate:
            crc = format((int(crc, 16) ^ 0x1) & 0xFFFFFFFF, "08x")
        done = sess.produced(now) >= sess.duration * RATE and sess.cursor[sensor] >= sess.duration * RATE
        head = json.dumps({"session_id": sess.id, "sensor": sensor, "first_index": first,
                           "count": len(rows), "lost": lost, "session_end": done, "crc32": crc})
        return head[:-1] + ', "data": ' + data_text + "}\n"

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", args.port))
    srv.listen(1)
    print(f"fake mock on {args.port} mode={args.mode} seed={args.seed}", flush=True)
    while True:
        conn, _ = srv.accept()
        f = conn.makefile("rwb")
        try:
            for line in f:
                if args.pause_every and args.pause_len:
                    phase = (time.monotonic() - t0) % args.pause_every
                    if phase > args.pause_every - args.pause_len:
                        time.sleep(args.pause_every - phase)
                with lock:
                    out = handle(json.loads(line)).encode()
                f.write(out)
                if args.dup_rate and rnd.random() < args.dup_rate:
                    f.write(out)
                f.flush()
        except (OSError, ValueError):
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass


if __name__ == "__main__":
    main()
