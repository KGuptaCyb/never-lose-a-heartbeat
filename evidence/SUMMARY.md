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

LATENCY_PLACEHOLDER
