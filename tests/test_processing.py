"""Processing tests. Run: python3 -m unittest discover tests
Also writes tests/golden_cases.json, which the TypeScript port must reproduce."""
import json
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))

from pipeline import processing as P  # noqa: E402
from fake_mock import Signal  # noqa: E402


def build(seconds, holes1=(), holes2=(), mode="signal"):
    sig = Signal(mode, 7)
    n = seconds * 400
    d1 = {i: sig.sample(1, i) for i in range(n) if not any(a <= i <= b for a, b in holes1)}
    d2 = {i: sig.sample(2, i) for i in range(n) if not any(a <= i <= b for a, b in holes2)}
    return n, d1, d2


def run_all(n, d1, d2):
    data = {1: d1, 2: d2}

    def load(sensor, lo, hi):
        src = data[sensor]
        return {i: src[i] for i in range(max(0, lo), hi) if i in src}

    state = P.new_state()
    known = {1: n, 2: n}
    ended = {1: True, 2: True}
    out = []
    while P.ready(state["next_window"], known, ended):
        w = state["next_window"]
        out.append(dict(window=w, **P.process_window(state, w, load)))
    return out


class ProcessingTest(unittest.TestCase):
    def test_heart_rate_follows_schedule(self):
        n, d1, d2 = build(150)
        res = run_all(n, d1, d2)
        self.assertEqual(len(res), 150)
        self.assertIsNone(res[3]["heart_rate"])
        self.assertAlmostEqual(res[60]["heart_rate"], 60, delta=1)
        self.assertAlmostEqual(res[140]["heart_rate"], 90, delta=1.5)
        self.assertTrue(res[10]["complete"])

    def test_gap_makes_rate_null_and_counts_missing(self):
        n, d1, d2 = build(30, holes1=[(4000, 4099)], holes2=[(8000, 8399)])
        res = run_all(n, d1, d2)
        self.assertEqual(res[10]["missing_1"], 100)
        self.assertFalse(res[10]["complete"])
        for w in range(10, 15):
            self.assertIsNone(res[w]["heart_rate"])
        self.assertEqual(res[20]["missing_2"], 400)
        self.assertIsNone(res[20]["magnitude"])
        self.assertEqual(res[20]["mean"]["sensor2"], [None, None])
        self.assertIsNotNone(res[17]["heart_rate"])

    def test_ramp_stats(self):
        n, d1, d2 = build(2, mode="ramp")
        res = run_all(n, d1, d2)
        # sensor1 ch1 = 3i+1 for i in 0..399 -> mean 3*199.5+1
        self.assertEqual(res[0]["mean"]["sensor1"][0], 599.5)
        self.assertEqual(res[0]["min"]["sensor1"][0], 1)
        self.assertEqual(res[0]["max"]["sensor1"][0], 3 * 399 + 1)

    def test_peaks_match_naive_definition(self):
        n, d1, _ = build(30, holes1=[(3000, 3010), (7000, 7799), (9500, 9500)])
        ecg = {i: v[1] for i, v in d1.items()}
        naive, last = [], None
        for i in range(n):
            if any(j not in ecg for j in range(i - 800, i)) or i < 800 or i not in ecg or i + 1 not in ecg:
                continue
            v = ecg[i]
            if v > ecg[i - 1] and v > ecg[i + 1] and v > max(ecg[j] for j in range(i - 800, i)) / 2 \
                    and (last is None or i - last >= 100):
                naive.append(i)
                last = i
        state = P.new_state()
        P.scan_peaks(state, ecg, 5000)   # incremental, in two steps
        P.scan_peaks(state, ecg, n)
        self.assertEqual(state["peaks"], naive)

    def test_write_golden(self):
        cases = []
        for name, secs, h1, h2 in [("clean", 40, [], []),
                                   ("gaps", 40, [(5000, 5050), (9000, 9799)], [(400, 1199)])]:
            n, d1, d2 = build(secs, h1, h2)
            cases.append({"name": name, "n": n,
                          "s1": [[i] + d1[i] for i in sorted(d1)],
                          "s2": [[i] + d2[i] for i in sorted(d2)],
                          "expected": run_all(n, d1, d2)})
        with open(os.path.join(ROOT, "tests", "golden_cases.json"), "w") as f:
            json.dump(cases, f, separators=(",", ":"))


if __name__ == "__main__":
    unittest.main()
