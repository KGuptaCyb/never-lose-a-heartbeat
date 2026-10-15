#!/usr/bin/env python3
"""Measure pipeline resource use on the device (Linux /proc).

Usage: sudo python3 scripts/measure_resources.py --seconds 3600 --iface eth0
Reports average CPU (% of one core, summed over collector + sync), peak memory
(max RSS of each process), MB written to disk (sectors written to the block
device, whole system, plus the per-process write_bytes counter for reference)
and MB sent over the network (interface tx bytes; includes all traffic).
"""
import argparse
import json
import os
import subprocess
import time

HZ = os.sysconf("SC_CLK_TCK")


def pid_of(unit):
    out = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", unit], capture_output=True, text=True)
    pid = int(out.stdout.strip() or 0)
    return pid or None


def cpu_ticks(pid):
    with open(f"/proc/{pid}/stat") as f:
        parts = f.read().rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])


def rss_kb(pid):
    with open(f"/proc/{pid}/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1])
    return 0


def write_bytes(pid):
    with open(f"/proc/{pid}/io") as f:
        for line in f:
            if line.startswith("write_bytes:"):
                return int(line.split()[1])
    return 0


def disk_sectors(dev):
    with open("/proc/diskstats") as f:
        for line in f:
            parts = line.split()
            if parts[2] == dev:
                return int(parts[9])  # sectors written (512 bytes)
    return 0


def net(iface):
    with open(f"/sys/class/net/{iface}/statistics/tx_bytes") as f:
        tx = int(f.read())
    with open(f"/sys/class/net/{iface}/statistics/rx_bytes") as f:
        rx = int(f.read())
    return tx, rx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=3600)
    ap.add_argument("--iface", default="eth0")
    ap.add_argument("--disk", default="vda", help="block device holding the database")
    ap.add_argument("--out", default="evidence/resources.json")
    args = ap.parse_args()
    units = ["pipeline-collect.service", "pipeline-sync.service"]
    # counters are per process; a restart resets them, so accumulate deltas per pid
    last = {}
    tot = {"cpu": 0, "wb": 0}
    peak = {u: 0 for u in units}
    tx0, rx0 = net(args.iface)
    d0 = disk_sectors(args.disk)
    t0 = time.monotonic()
    samples = []
    while time.monotonic() - t0 < args.seconds:
        for u in units:
            pid = pid_of(u)
            if not pid:
                continue
            try:
                c, w, r = cpu_ticks(pid), write_bytes(pid), rss_kb(pid)
            except OSError:
                continue
            pc, pw = last.get(pid, (c, w))
            tot["cpu"] += c - pc
            tot["wb"] += w - pw
            last[pid] = (c, w)
            peak[u] = max(peak[u], r)
        el = time.monotonic() - t0
        samples.append({"t": round(el), "cpu_pct": round(100 * tot["cpu"] / HZ / max(el, 1), 2)})
        time.sleep(5)
    el = time.monotonic() - t0
    tx1, rx1 = net(args.iface)
    d1 = disk_sectors(args.disk)
    with open("/proc/cpuinfo") as f:
        cores = sum(1 for l in f if l.startswith("processor"))
    with open("/proc/meminfo") as f:
        mem = int(f.readline().split()[1]) // 1024
    res = {
        "environment": f"{os.uname().machine}, {cores} cores, {mem} MB RAM, {os.uname().release}",
        "seconds": round(el),
        "avg_cpu_percent_of_one_core": round(100 * tot["cpu"] / HZ / el, 2),
        "peak_rss_mb": {u: round(v / 1024, 1) for u, v in peak.items()},
        "disk_written_mb_block_device": round((d1 - d0) * 512 / 1e6, 1),
        "disk_written_mb_proc_io_counter": round(tot["wb"] / 1e6, 1),
        "network_sent_mb": round((tx1 - tx0) / 1e6, 1),
        "network_received_mb": round((rx1 - rx0) / 1e6, 1),
    }
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump({**res, "cpu_timeline": samples}, f, indent=1)
    print(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
