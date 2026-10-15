"""Section 3 processing logic. Mirrored line-for-line in
cloud/supabase/functions/pipeline/processing.ts; both must pass
tests/golden_cases.json.

A window is processed only once every sample it depends on is *decided*:
either saved or recorded as lost. Peak detection is sequential over the whole
session (the "previous peak" rule carries state), so the processor keeps
{next_window, scan_next, last_peak, peaks} as persistent state.
"""
import math
from collections import deque

W = 400            # samples per window (1 s)
LOOKBACK = 800     # samples before a peak that must be available
REFRACTORY = 100   # min samples between peaks


def r1(x):
    return None if x is None else math.floor(x * 10 + 0.5) / 10


def new_state():
    return {"next_window": 0, "scan_next": 0, "last_peak": None, "peaks": []}


def window_stats(s1, s2, w):
    """s1/s2: dict index -> values for the window's indexes."""
    out = {}
    lo = w * W
    for sensor, data, nch in ((1, s1, 3), (2, s2, 2)):
        rows = [data[i] for i in range(lo, lo + W) if i in data]
        out["missing_%d" % sensor] = W - len(rows)
        if rows:
            out.setdefault("mean", {})["sensor%d" % sensor] = [r1(sum(r[c] for r in rows) / len(rows)) for c in range(nch)]
            out.setdefault("min", {})["sensor%d" % sensor] = [min(r[c] for r in rows) for c in range(nch)]
            out.setdefault("max", {})["sensor%d" % sensor] = [max(r[c] for r in rows) for c in range(nch)]
        else:
            out.setdefault("mean", {})["sensor%d" % sensor] = [None] * nch
            out.setdefault("min", {})["sensor%d" % sensor] = [None] * nch
            out.setdefault("max", {})["sensor%d" % sensor] = [None] * nch
        if sensor == 2:
            # plain left-to-right addition (not sum(), which is compensated in
            # Python 3.12+) so the TypeScript port gives bit-identical results
            acc = 0.0
            for r in rows:
                acc += math.sqrt(r[0] * r[0] + r[1] * r[1])
            out["magnitude"] = r1(acc / len(rows)) if rows else None
    out["complete"] = out["missing_1"] == 0 and out["missing_2"] == 0
    return out


def scan_peaks(state, ecg, upto):
    """Test candidates i in [state.scan_next, upto). ecg: dict index -> ch2 value,
    must contain every available index in [scan_next-800, upto+1)."""
    start = state["scan_next"]
    if start >= upto:
        return
    dq = deque()       # indexes in (i-800 .. i-1) with decreasing values
    missing = 0        # missing count in window [i-800, i-1]
    lo = start - LOOKBACK
    for j in range(lo, start):
        if j < 0 or j not in ecg:
            missing += 1
        else:
            while dq and ecg[dq[-1]] <= ecg[j]:
                dq.pop()
            dq.append(j)
    last = state["last_peak"]
    peaks = state["peaks"]
    for i in range(start, upto):
        if missing == 0 and i in ecg and (i + 1) in ecg:
            v = ecg[i]
            if (v > ecg[i - 1] and v > ecg[i + 1] and v > ecg[dq[0]] / 2
                    and (last is None or i - last >= REFRACTORY)):
                peaks.append(i)
                last = i
        # slide window: add i, drop i-800
        out_j = i - LOOKBACK
        if out_j < 0 or out_j not in ecg:
            missing -= 1
        if dq and dq[0] == out_j:
            dq.popleft()
        if i in ecg:
            while dq and ecg[dq[-1]] <= ecg[i]:
                dq.pop()
            dq.append(i)
        else:
            missing += 1
    state["last_peak"] = last
    state["scan_next"] = upto


def heart_rate(state, s1_avail, w):
    """s1_avail: set of available sensor-1 indexes in [400(w-4), 400(w+1))."""
    if w < 4:
        return None
    lo, hi = W * (w - 4), W * (w + 1)
    if len(s1_avail) != hi - lo:
        return None
    pk = [p for p in state["peaks"] if lo <= p < hi]
    if len(pk) < 2:
        return None
    mean_gap_s = (pk[-1] - pk[0]) / (len(pk) - 1) / W
    return r1(60 / mean_gap_s)


def ready(w, known, ended):
    """known[s]: every index < known[s] is decided. ended[s]: nothing more will come."""
    need = {1: W * (w + 1) + 1, 2: W * (w + 1)}
    for s in (1, 2):
        if known[s] < need[s] and not ended[s]:
            return False
    if ended[1] and ended[2] and W * w >= max(known[1], known[2]):
        return False  # past the end of the session
    return True


def process_window(state, w, load):
    """Advance peak scanning through window w and return its outputs.
    load(sensor, lo, hi) -> dict index -> values (available samples only)."""
    end = W * (w + 1)
    scan_lo = max(0, state["scan_next"] - LOOKBACK)
    s1 = load(1, scan_lo, end + 1)
    ecg = {i: v[1] for i, v in s1.items()}
    scan_peaks(state, ecg, end)
    s2 = load(2, W * w, end)
    out = window_stats({i: v for i, v in s1.items() if i >= W * w and i < end}, s2, w)
    hr_lo = W * (w - 4)
    if w >= 4 and scan_lo > hr_lo:
        avail = set(load(1, hr_lo, end).keys())
    else:
        avail = {i for i in s1 if hr_lo <= i < end}
    out["heart_rate"] = heart_rate(state, avail, w)
    state["peaks"] = [p for p in state["peaks"] if p >= W * (w - 3)]
    state["next_window"] = w + 1
    return out
