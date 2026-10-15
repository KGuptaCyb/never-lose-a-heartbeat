#!/usr/bin/env python3
"""Summarise `pipeline results` output for the evidence folder.

Usage: pipeline results --session ID | python3 tools/results_report.py
Reports cloud latency (device receipt of the last needed sample -> cloud
result), heart-rate values seen, and whether offline (device) and online
(cloud) results agree for windows that have both.
"""
import json
import sys

IGNORE = {"mode", "processed_at", "device_received_at"}


def main():
    rows = [json.loads(l) for l in sys.stdin if l.strip()]
    online = {r["window"]: r for r in rows if r["mode"] == "online"}
    offline = {r["window"]: r for r in rows if r["mode"] == "offline"}
    lat = sorted(r["processed_at"] - r["device_received_at"] for r in online.values()
                 if r.get("device_received_at") and r.get("processed_at"))
    out = {"windows_online": len(online), "windows_offline": len(offline)}
    if lat:
        pick = lambda q: round(lat[min(len(lat) - 1, int(q * len(lat)))], 2)  # noqa: E731
        out["cloud_latency_s"] = {"median": pick(0.5), "p95": pick(0.95), "p99": pick(0.99),
                                  "max": round(lat[-1], 2),
                                  "within_5s_pct": round(100 * sum(x <= 5 for x in lat) / len(lat), 2)}
    hr = {}
    for r in online.values():
        hr[r["heart_rate"]] = hr.get(r["heart_rate"], 0) + 1
    out["heart_rate_counts"] = {str(k): v for k, v in sorted(hr.items(), key=lambda kv: (kv[0] is None, kv[0] or 0))}
    both = sorted(set(online) & set(offline))
    diff = [w for w in both if {k: v for k, v in online[w].items() if k not in IGNORE}
            != {k: v for k, v in offline[w].items() if k not in IGNORE}]
    out["windows_with_both_modes"] = len(both)
    out["online_offline_mismatches"] = diff[:20]
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    main()
