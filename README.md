# Never Lose a Heartbeat

A data pipeline for a Raspberry Pi-class bedside device. It saves every sensor sample durably on the device, and streams the data to a free cloud service (Supabase) where it is processed within seconds. When the network drops, it processes on the device instead. After an outage it catches the cloud up, so the cloud ends up holding every sample exactly once.

- **Design note:** [docs/DESIGN.md](docs/DESIGN.md) covers the architecture, what is guaranteed, and the power-cut loss bound.
- **Evidence:** [evidence/](evidence/) holds the failure-test logs and the resource figures.

> **About the mock service.** The brief's download link for `biosignal-mock` was blank ("Download: __"), and no binary was sent with the assessment. This pipeline was therefore developed and tested against **`tools/fake_mock.py`**, a stand-in I wrote from the protocol in section 6. It implements:
> - signal and ramp modes, `--seed` and `--duration`;
> - the 10 s buffer, `lost` ranges, `session_end`, and CRC32 over the raw `data` text;
> - fault injection: corrupt replies, duplicate replies, pauses, sensor dropout, restart.
>
> To use the real mock, set `MOCK_CMD` (or run it yourself on port 7100). No code changes are needed. `tools/check_export.py` is a stand-in for `check_ground_truth` that works for ramp mode.

## Layout

| Path | What |
|---|---|
| `pipeline/collector.py` | Reads the mock, checks the CRC, removes duplicates, re-reads, accounts for losses, writes durably to SQLite |
| `pipeline/store.py` | SQLite schema (WAL, `synchronous=FULL`) and the tiling invariant |
| `pipeline/sync.py` | Link monitor, online/offline switching, idempotent upload and catch-up, offline processing |
| `pipeline/processing.py` | Section 3 logic: windows, statistics, streaming peak detector, heart rate |
| `pipeline/cli.py` | The `pipeline` command |
| `cloud/supabase/functions/pipeline/` | Edge Function (`index.ts`) and the TypeScript port of the processing logic (`processing.ts`) |
| `tests/` | Processing unit tests, a naive-definition peak check, and the Python↔TypeScript golden test |
| `scripts/` | Failure tests (on the device), power-cut and reboot tests (from the host), resource measurement |
| `deploy/` | `install.sh` and the systemd units |

## Install on a fresh board (Raspberry Pi OS / Ubuntu, ARM64)

The device needs only `python3` (3.9+). There are no pip dependencies; it uses only the standard library.

1. Copy the example config and fill it in:
   ```bash
   cp deploy/pipeline.env.example pipeline.env
   ```
   Set `CLOUD_URL`, `CLOUD_ANON_KEY` and `DEVICE_TOKEN`. Optionally set `MOCK_CMD`.
2. Run the installer:
   ```bash
   sudo ./deploy/install.sh pipeline.env
   ```

The installer copies the code to `/opt/pipeline`, writes `/etc/pipeline.env` (mode 600), creates `/usr/local/bin/pipeline`, and enables the services `pipeline-collect`, `pipeline-sync` and, if `MOCK_CMD` is set, `biosignal-mock`. The services start on boot and restart within 1 s.

## Cloud setup (Supabase free tier, no card)

```bash
npx supabase login
npx supabase secrets set DEVICE_TOKEN=<same token as the device> --project-ref <ref>
npx supabase functions deploy pipeline --project-ref <ref> --no-verify-jwt --use-api --workdir cloud
```

The function creates its tables on first use. The device authenticates with the `x-device-token` header. Row-level security is enabled on all tables, so the public key alone cannot read the data.

## Commands

```bash
pipeline status
pipeline export --session ID --from device
pipeline export --session ID --from cloud
pipeline results --session ID
pipeline sessions
```

- **`status`** prints one JSON line: current mode, samples saved, samples in the cloud, backlog, and loss count.
- **`export`** prints one JSON line per sample, then one line per loss, in the format given in the brief.
- **`results`** prints one JSON line per window and mode. It merges the device's offline results with the cloud's online results.
- **`sessions`** lists the session IDs.

## Tests

```bash
python3 -m unittest discover tests
node --experimental-strip-types tests/golden.test.ts
sudo scripts/failure_tests.sh all 60
scripts/host_power_tests.sh
sudo python3 scripts/measure_resources.py --seconds 3600 --iface eth0
```

These run, in order:
1. processing tests, including a check against a naive implementation of the peak definition;
2. the TypeScript port, which must give identical output on the golden cases;
3. on the device: network cut, `kill -9`, disk full, clock change, then full verification;
4. on the host (Lima VM): power cut (forced VM stop) and reboot;
5. on the device: resource figures over 1 hour.

## Environment used

- **Device:** Lima VM using Apple Virtualization (vz) on an M1 MacBook Air. Ubuntu 26.04 ARM64 (aarch64), limited to **4 vCPUs and 2 GB RAM**, with a 20 GB disk.
- **Cloud:** Supabase free tier (Edge Functions + Postgres, ap-south-1).

## AI tools disclosure

This project was built with the help of **Claude (Anthropic) via Claude Code**. It was used for the code, the tests, the stand-in mock and drafting the docs. I reviewed the design decisions, ran every test shown in `evidence/`, and can explain and modify any part of it.
