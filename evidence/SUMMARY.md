# Evidence summary

**Environment.** Lima VM (Apple Virtualization) on an M1 MacBook Air, running Ubuntu 26.04 **aarch64**, limited to **4 vCPUs and 2 GB RAM** with a 20 GB disk. Python 3.14.

**Cloud.** Supabase free tier (Edge Function + Postgres).

**Mock.** `tools/fake_mock.py`, a stand-in built from section 6 of the brief, because the real mock's download link was blank. The tests used ramp mode with 2% corrupt replies and 2% duplicate replies.

## Failure tests

Run 2 is the run after the fixes: [failure_tests_run2.log](failure_tests_run2.log), [power_tests_20261016_011208.log](power_tests_20261016_011208.log) and [verify_after_power_tests.log](verify_after_power_tests.log).

| Test | Result | Criterion |
|---|---|---|
| Network cut, 120 s (packets silently dropped) | Switched to offline after **6.9 s**, back to online after **3.0 s** | 5 (≤ 10 s) ✅ |
| Catch-up after a 120 s outage (≈102k samples) | Backlog cleared in **3.3 s** after reconnecting, about 36× real time | 6 ✅ |
| `kill -9` of both processes, 3 times | Restarted by systemd within 1 s each time. No loss, no duplicates. | 3 ✅ |
| Disk 100% full for 30 s | Reserve file released and saving continued. No loss. | 3 ✅ |
| Clock moved +3 h, then −1 day | No effect on data or mode. Only the informational latency field is affected. | ✅ |
| Power cut (forced VM stop) | Old session's unread tail listed as lost with `to_index: null`. New session started automatically after boot. | 2, 3 ✅ |
| Reboot | Services start on boot, and a new session is picked up | 3 ✅ |
| Mock corrupt and duplicate replies (2% each, whole run) | All corrupt replies rejected by CRC, all duplicates dropped | 1 ✅ |
| Final verification across 4 sessions (≈930k samples) | Device export matches the ramp formula exactly: no duplicates, no wrong values, no silent gaps. The cloud copy is **identical** for every closed session. | 1, 2, 6 ✅ |
| Offline (device) vs online (cloud) results, 224 windows that have both | **Identical** | 7 ✅ |
| Python vs TypeScript processing (golden test, 80 windows with gaps) | Identical | 7 ✅ |

Run 1 ([failure_tests_run1_before_fixes.log](failure_tests_run1_before_fixes.log)) found 2 real problems, both fixed:
- **Switching to offline took 24 s.** Each failed HTTPS call waited the full timeout once per resolved IP address. The fix was a hard deadline per call, plus a quick health check to confirm a failure.
- **Cloud database connections ran out.** The fix was one short-lived connection per request.

## Resources over 1 hour, online mode, signal mode

RESOURCES_PLACEHOLDER

## Online result latency, same hour

This was a signal-mode session of 66 minutes (4,000 windows), session `643840b8`. The full report is in [latency_signal_session.json](latency_signal_session.json).

Latency here is measured from the moment the device received the last sample a window depends on, to the moment the cloud wrote that window's result.

| Metric | Value |
|---|---|
| Windows with an online (cloud) result | 4,000 of 4,000 |
| Latency, median / p95 / p99 / max | **0.46 s / 0.79 s / 1.34 s / 4.8 s** |
| Windows within 5 s | **100%** (criterion 4 ✅) |
| Complete windows | 4,000 of 4,000 |
| Heart rate | 60 bpm (minutes 0–2), 90 (2–4), 72 (4–6), repeating, as the mock specifies. Values in between (62.6–86.4) appear only in the windows whose 5 s span straddles a rate change. `null` only for windows 0–3. |

## Extra: disk full for longer than the mock's buffer, with no reserve left

This test ran on macOS using an 8 MB RAM disk.
- The disk was kept full for 25 s, which is longer than the mock's 10 s buffer, so samples overflowed.
- The overflow was recorded as **lost, with the reason `buffer_overflow (device disk was full)`**: 6,054 samples per sensor, as one range.
- There were no silent gaps and no wrong values (`check_export.py`: OK).
