"""`pipeline` command: collect | sync | status | export | results."""
import argparse
import json
import sys

from . import config
from .store import Store


def export_device(store, sid, out):
    for sensor in (1, 2):
        chunks, _ = store.iter_records(sid, sensor)
        for first, _count, data in chunks:
            for k, values in enumerate(json.loads(data)):
                out.write(json.dumps({"sensor": sensor, "index": first + k, "values": values}) + "\n")
    for sensor in (1, 2):
        _, losses = store.iter_records(sid, sensor)
        for frm, to, reason in losses:
            out.write(json.dumps({"sensor": sensor, "from_index": frm, "to_index": to, "reason": reason}) + "\n")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pipeline")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("collect", help="run the collector (reads the mock, saves to disk)")
    sub.add_parser("sync", help="run the sync agent (link monitor, upload, offline processing)")
    sub.add_parser("status")
    p = sub.add_parser("export")
    p.add_argument("--session")
    p.add_argument("--from", dest="source", choices=["device", "cloud"], default="device")
    p = sub.add_parser("results")
    p.add_argument("--session")
    p.add_argument("--from", dest="source", choices=["all", "device", "cloud"], default="all")
    p = sub.add_parser("sessions")
    args = ap.parse_args(argv)

    if args.cmd == "collect":
        from .collector import main as run
        return run(config.DB_PATH, config.MOCK_HOST, config.MOCK_PORT)
    if args.cmd == "sync":
        from .sync import main as run
        return run(config.DB_PATH)

    store = Store(config.DB_PATH)
    sid = getattr(args, "session", None) or store.latest_session()
    out = sys.stdout

    if args.cmd == "sessions":
        for s in store.sessions():
            print(s)
    elif args.cmd == "status":
        from .sync import status
        print(json.dumps(status(store)))
    elif args.cmd == "export":
        if sid is None:
            sys.exit("no session yet")
        if args.source == "device":
            export_device(store, sid, out)
        else:
            from .cloud import Cloud
            Cloud().export(sid, out)
    elif args.cmd == "results":
        if sid is None:
            sys.exit("no session yet")
        rows = []
        if args.source in ("all", "device"):
            for (payload,) in store.db.execute(
                    "SELECT payload FROM results WHERE session_id=? ORDER BY window", (sid,)):
                rows.append(json.loads(payload))
        if args.source in ("all", "cloud"):
            from .cloud import Cloud
            try:
                rows.extend(Cloud().results(sid))
            except Exception as e:  # offline: show what the device has, say so on stderr
                print(f"cloud results unavailable: {e}", file=sys.stderr)
        rows.sort(key=lambda r: (r["window"], r["mode"]))
        for r in rows:
            out.write(json.dumps(r) + "\n")


if __name__ == "__main__":
    main()
