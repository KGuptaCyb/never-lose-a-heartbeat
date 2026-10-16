#!/bin/bash
# Self-running demo for the video (run on the Mac host; VM "pi" must be running).
# Shows: network cut, process kill, power cut, then the cloud copy and results.
set -u
VM=${VM:-pi}
B=$'\e[1;36m'; Y=$'\e[1;33m'; G=$'\e[1;32m'; N=$'\e[0m'
pi() { limactl shell "$VM" -- "$@"; }
say() { echo; echo "${B}════════════════════════════════════════════════════════════${N}"; echo "${B}  $*${N}"; echo "${B}════════════════════════════════════════════════════════════${N}"; }
note() { echo "${Y}→ $*${N}"; }
watch_status() {  # watch_status <seconds>
  for _ in $(seq 1 "$1"); do
    printf '%s  ' "$(date +%T)"; pi pipeline status 2>/dev/null | python3 -c \
      "import json,sys; s=json.load(sys.stdin); print(f\"mode={s['mode']:<8} saved={s['samples_saved']:>9}  in_cloud={s['samples_in_cloud']:>9}  backlog={s['backlog']:>7}  losses={s['losses']}  offline_results={s['offline_results']}\")"
    sleep 1
  done
}

say "Never Lose a Heartbeat: pipeline demo"
note "Device: ARM64 VM limited like a Raspberry Pi 4"
pi bash -c 'echo "  arch=$(uname -m)  cores=$(nproc)  memory=$(free -m | awk "/Mem/{print \$2}") MB"'
note "Services: mock sensors, collector (saves to disk), sync (cloud + offline processing)"
pi sudo systemctl start biosignal-mock
pi bash -c 'for u in biosignal-mock pipeline-collect pipeline-sync; do echo "  $u: $(systemctl is-active $u)"; done'
watch_status 6

say "1) NETWORK CUT: traffic to the cloud silently dropped"
pi sudo iptables -I OUTPUT -p tcp --dport 443 -j DROP
note "Expect: mode -> offline within 10 s; saving continues; backlog grows; device processes windows itself"
watch_status 22
say "   Network restored"
pi sudo iptables -D OUTPUT -p tcp --dport 443 -j DROP
note "Expect: mode -> online within 10 s; backlog drains to ~0 (catch-up, every sample exactly once)"
watch_status 14

say "2) PROCESS KILL: kill -9 collector and sync"
pi sudo systemctl kill -s KILL pipeline-collect pipeline-sync
note "Expect: systemd restarts both within ~1 s; losses unchanged (mock still holds the last 10 s)"
watch_status 10

say "3) POWER CUT: VM forced off (like pulling the plug)"
OLD=$(pi pipeline sessions | tail -1)
note "Current session: $OLD"
limactl stop --force "$VM" >/dev/null 2>&1
echo "  power OFF at $(date +%T)"
sleep 5
note "Powering back on..."
limactl start "$VM" >/dev/null 2>&1
echo "  power ON at $(date +%T)"
until pi true 2>/dev/null; do sleep 2; done
sleep 8
note "Services came back on boot; the mock lost power too, so a new session started"
watch_status 6
note "Old session's unread tail is LISTED as lost (never silent), to_index null = end unknown:"
pi pipeline export --session "$OLD" --from device | grep from_index | tail -2

say "4) CLOUD COPY = DEVICE COPY, and results"
note "Comparing every line of session $OLD on the device and in the cloud..."
pi bash -c "cd /tmp && pipeline export --session $OLD --from device | sort > d.jsonl && pipeline export --session $OLD --from cloud | sort > c.jsonl && echo \"  device: \$(wc -l < d.jsonl) lines   cloud: \$(wc -l < c.jsonl) lines\" && (cmp -s d.jsonl c.jsonl && echo '  ${G}IDENTICAL${N}' || echo '  DIFFERENT'); rm -f d.jsonl c.jsonl"
note "Latest per-second results (heart rate from sensor 1 ECG; mode online = processed in cloud):"
pi pipeline results | tail -2 | python3 -c "
import json,sys
for l in sys.stdin:
    r=json.loads(l); print(f\"  window {r['window']}: mode={r['mode']} heart_rate={r['heart_rate']} complete={r['complete']} magnitude={r['magnitude']}\")"
say "Done: every sample is on disk exactly once or listed as lost with a reason"
