#!/bin/bash
# Failure tests run ON THE DEVICE (as root) while the services are running.
# Usage: sudo scripts/failure_tests.sh [all|net|kill|disk|clock|verify] [outage_seconds]
# Power cut and reboot are driven from the host: scripts/host_power_tests.sh
set -u
WHAT=${1:-all}
OUTAGE=${2:-60}
EV=${EVIDENCE:-/opt/pipeline/evidence}
mkdir -p "$EV"
LOG="$EV/failure_tests_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

now() { date +%s.%N; }
field() { pipeline status | python3 -c "import json,sys;print(json.load(sys.stdin)['$1'])"; }
wait_for() {  # wait_for <field> <value> <timeout>  -> prints seconds waited
  local t0; t0=$(now)
  while [ "$(field "$1")" != "$2" ]; do
    sleep 0.25
    if (( $(echo "$(now) - $t0 > $3" | bc) )); then echo "TIMEOUT"; return 1; fi
  done
  echo "$(now) - $t0" | bc
}
wait_below() {  # wait_below <field> <max> <timeout>: live data keeps a small backlog in flight
  local t0; t0=$(now)
  while [ "$(field "$1")" -gt "$2" ]; do
    sleep 0.25
    if (( $(echo "$(now) - $t0 > $3" | bc) )); then echo "TIMEOUT"; return 1; fi
  done
  echo "$(now) - $t0" | bc
}
block()   { iptables -I OUTPUT -p tcp --dport 443 -j DROP; }
unblock() { iptables -D OUTPUT -p tcp --dport 443 -j DROP; }

test_net() {
  echo "== NETWORK CUT ($OUTAGE s), traffic to the cloud dropped silently"
  pipeline status
  block; echo "cut at $(date +%T)"
  echo "switched to offline after: $(wait_for mode offline 30) s (limit 10)"
  sleep "$OUTAGE"
  pipeline status
  unblock; echo "restored at $(date +%T)"
  echo "switched to online after: $(wait_for mode online 30) s (limit 10)"
  echo "backlog cleared (< 1000 samples, i.e. only live data in flight) after: $(wait_below backlog 1000 1800) s"
  pipeline status
}

test_kill() {
  echo "== PROCESS KILL (kill -9 collector and sync, 3 times)"
  for i in 1 2 3; do
    systemctl kill -s KILL pipeline-collect.service pipeline-sync.service
    echo "killed at $(date +%T)"; sleep 5
    systemctl is-active pipeline-collect.service pipeline-sync.service | tr '\n' ' '; echo
  done
  pipeline status
}

test_disk() {
  echo "== DISK FULL for 30 s"
  local dir; dir=$(dirname "$(grep PIPELINE_DB /etc/pipeline.env | cut -d= -f2)")
  local avail; avail=$(df --output=avail -B1 "$dir" | tail -1)
  fallocate -l $((avail - 1024 * 1024)) "$dir/fill.bin" && echo "disk filled at $(date +%T)"
  df -h "$dir" | tail -1
  sleep 30
  rm -f "$dir/fill.bin"; echo "space freed at $(date +%T)"
  sleep 15
  journalctl -u pipeline-collect --since "-60s" --no-pager | grep -i "disk\|loss" | tail -5
  pipeline status
}

test_clock() {
  echo "== CLOCK CHANGE (+3 h, then -1 day, then back)"
  timedatectl set-ntp false
  date -s "+3 hours" >/dev/null; echo "clock now $(date)"; sleep 20
  date -s "-1 day" >/dev/null; echo "clock now $(date)"; sleep 20
  timedatectl set-ntp true; sleep 5; echo "clock restored $(date)"
  pipeline status
}

verify() {
  echo "== VERIFY: every session, device export vs ramp formula, cloud copy vs device copy"
  echo "waiting for the cloud to catch up: $(wait_below backlog 1000 1800) s"
  for s in $(pipeline sessions); do
    echo "-- session $s"
    pipeline export --session "$s" --from device | sort > /tmp/dev.jsonl
    python3 /opt/pipeline/tools/check_export.py < /tmp/dev.jsonl
    pipeline export --session "$s" --from cloud | sort > /tmp/cloud.jsonl
    echo "cloud copy check:"; python3 /opt/pipeline/tools/check_export.py < /tmp/cloud.jsonl
    # the session may still be recording, so the cloud can be slightly ahead of the device snapshot
    missing=$(comm -23 /tmp/dev.jsonl /tmp/cloud.jsonl | wc -l)
    extra=$(comm -13 /tmp/dev.jsonl /tmp/cloud.jsonl | wc -l)
    echo "device lines missing from cloud: $missing; cloud lines newer than the device snapshot: $extra"
  done
}

case "$WHAT" in
  net) test_net ;;
  kill) test_kill ;;
  disk) test_disk ;;
  clock) test_clock ;;
  verify) verify ;;
  all) test_net; test_kill; test_disk; test_clock; verify ;;
esac
echo "log: $LOG"
