#!/bin/bash
# Power cut and reboot of the Lima VM "pi", driven from the host (macOS).
# A forced stop kills the VM without flushing anything, like pulling the plug.
set -u
VM=${VM:-pi}
EV=$(cd "$(dirname "$0")/.." && pwd)/evidence
mkdir -p "$EV"
LOG="$EV/power_tests_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1
st() { limactl shell "$VM" pipeline status; }

echo "== POWER CUT"
st
limactl stop --force "$VM"; echo "power cut at $(date +%T)"
sleep 15
limactl start "$VM" >/dev/null 2>&1; echo "powered on at $(date +%T)"
sleep 20; st
limactl shell "$VM" sudo journalctl -u pipeline-collect -b --no-pager | grep -i "session\|loss" | tail -5

echo "== REBOOT"
limactl shell "$VM" sudo systemctl reboot || true
sleep 5
until limactl shell "$VM" true 2>/dev/null; do sleep 2; done
echo "back at $(date +%T)"; sleep 15; st
echo "log: $LOG"
