#!/usr/bin/env python3
"""Development check for a ramp-mode export (stand-in for check_ground_truth).

Verifies: every sample value matches the ramp formula, no index appears twice,
and samples + losses tile [0, end) for each sensor with no silent gap.
Usage: pipeline export ... | python3 tools/check_export.py
"""
import json
import sys

MOD = 8_388_608


def main():
    seen = {1: {}, 2: {}}
    losses = {1: [], 2: []}
    bad = 0
    for line in sys.stdin:
        r = json.loads(line)
        s = r["sensor"]
        if "index" in r:
            i = r["index"]
            if i in seen[s]:
                print("DUPLICATE", s, i)
                bad += 1
            seen[s][i] = r["values"]
            k = 3 if s == 1 else 2
            want = [(k * i + c) % MOD for c in range(1, (3 if s == 1 else 2) + 1)]
            if r["values"] != want:
                print("WRONG VALUE", s, i, r["values"], want)
                bad += 1
        else:
            losses[s].append((r["from_index"], r["to_index"], r["reason"]))
    for s in (1, 2):
        covered = set(seen[s])
        open_end = None
        for a, b, _ in losses[s]:
            if b is None:
                open_end = a
                continue
            for i in range(a, b + 1):
                if i in covered:
                    print("LOSS OVERLAPS SAMPLE", s, i)
                    bad += 1
                covered.add(i)
        end = max(covered) + 1 if covered else 0
        gaps = [i for i in range(end) if i not in covered]
        if gaps:
            print(f"SILENT GAP sensor {s}: {len(gaps)} samples, first {gaps[:5]}")
            bad += 1
        lost_n = sum((b - a + 1) for a, b, _ in losses[s] if b is not None)
        print(f"sensor {s}: {len(seen[s])} samples, {lost_n} lost in {len(losses[s])} ranges"
              + (f", open loss from {open_end}" if open_end is not None else "")
              + f", reasons {sorted({r for *_, r in losses[s]})}")
    print("OK" if not bad else f"FAILED ({bad} problems)")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
