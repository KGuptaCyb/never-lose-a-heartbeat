#!/bin/sh
# Install the pipeline on a fresh Raspberry Pi OS / Ubuntu (ARM64) board.
# Usage: sudo ./deploy/install.sh /path/to/pipeline.env
set -eu
ENV_SRC=${1:-.env}
SRC=$(cd "$(dirname "$0")/.." && pwd)
command -v python3 >/dev/null || { apt-get update && apt-get install -y python3; }
mkdir -p /opt/pipeline /var/lib/pipeline
cp -r "$SRC/pipeline" "$SRC/tools" /opt/pipeline/
install -m 600 "$ENV_SRC" /etc/pipeline.env
grep -q '^PIPELINE_DB=' /etc/pipeline.env || echo 'PIPELINE_DB=/var/lib/pipeline/pipeline.db' >> /etc/pipeline.env
cat > /usr/local/bin/pipeline <<'W'
#!/bin/sh
[ -r /etc/pipeline.env ] || exec sudo "$0" "$@"
set -a; . /etc/pipeline.env; set +a
cd /opt/pipeline && exec python3 -m pipeline.cli "$@"
W
chmod 755 /usr/local/bin/pipeline
cp "$SRC"/deploy/systemd/*.service /etc/systemd/system/
systemctl daemon-reload
if grep -q '^MOCK_CMD=' /etc/pipeline.env; then systemctl enable --now biosignal-mock.service; fi
systemctl enable --now pipeline-collect.service pipeline-sync.service
echo "installed; try: pipeline status"
