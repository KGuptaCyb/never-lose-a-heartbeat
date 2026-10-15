"""Minimal HTTP client for the cloud function (stdlib only)."""
import gzip
import json
import threading
import urllib.error
import urllib.parse
import urllib.request

from . import config


class CloudError(Exception):
    pass


class Cloud:
    def __init__(self, url=None):
        self.url = (url or config.CLOUD_URL).rstrip("/")
        if not self.url:
            raise CloudError("CLOUD_URL not configured")

    def _req(self, method, path, params=None, body=None, timeout=5.0):
        """Like _do, but with a hard overall deadline. urllib's timeout is per
        socket operation and per resolved address, so a silently dropped network
        could otherwise block for several times `timeout`. An abandoned call may
        still complete later; that is safe because ingest is idempotent."""
        box = {}

        def run():
            try:
                box["ok"] = self._do(method, path, params, body, timeout)
            except Exception as e:  # noqa: BLE001 - re-raised below
                box["err"] = e

        t = threading.Thread(target=run, daemon=True)
        t.start()
        t.join(timeout)
        if "ok" in box:
            return box["ok"]
        if "err" in box:
            raise box["err"]
        raise CloudError(f"no answer within {timeout:.1f}s")

    def _do(self, method, path, params, body, timeout):
        url = self.url + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"x-device-token": config.DEVICE_TOKEN}
        if config.CLOUD_ANON_KEY:
            headers["apikey"] = config.CLOUD_ANON_KEY
        data = None
        if body is not None:
            data = gzip.compress(json.dumps(body, separators=(",", ":")).encode(), compresslevel=6)
            headers["content-type"] = "application/octet-stream"
            headers["x-body-encoding"] = "gzip"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                payload = r.read()
                return json.loads(payload), len(data or b"") + len(payload)
        except urllib.error.HTTPError as e:
            raise CloudError(f"HTTP {e.code}: {e.read()[:300]!r}") from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise CloudError(str(e)) from None

    def health(self, timeout=2.0):
        return self._req("GET", "/health", timeout=timeout)[0]

    def ingest(self, sid, items, timeout=15.0):
        return self._req("POST", "/ingest", body={"session_id": sid, "items": items}, timeout=timeout)

    def results(self, sid):
        return self._req("GET", "/results", {"session": sid}, timeout=60)[0]

    def status(self, sid):
        return self._req("GET", "/status", {"session": sid}, timeout=5)[0]

    def export(self, sid, out, page=40_000):
        losses = []
        for sensor in (1, 2):
            frm = 0
            while True:
                res, _ = self._req("GET", "/export", {"session": sid, "sensor": sensor, "from": frm,
                                                      "to": frm + page}, timeout=60)
                losses = res["losses"]
                for first, rows in res["chunks"]:
                    if isinstance(rows, str):
                        rows = json.loads(rows)
                    for k, values in enumerate(rows):
                        out.write(json.dumps({"sensor": sensor, "index": first + k, "values": values}) + "\n")
                frm += page
                if frm >= res["known_next"] and not res["chunks"]:
                    break
        for l in losses:
            out.write(json.dumps({"sensor": l["sensor"], "from_index": int(l["from_index"]),
                                  "to_index": None if l["to_index"] is None else int(l["to_index"]),
                                  "reason": l["reason"]}) + "\n")
