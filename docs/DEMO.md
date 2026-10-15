# Demo video script (≤ 5 minutes)

Record the screen with **QuickTime → File → New Screen Recording**, or press **Cmd+Shift+5**.

Before you start:

1. The test mock was stopped overnight to save cloud storage. Start it again, then wait about 10 s:
   ```bash
   limactl shell pi -- sudo systemctl start biosignal-mock
   ```
   The old session will show one listed loss with `to_index: null`. That is correct: it is the "mock restart" case.

2. Open **two Terminal windows** side by side.

- **Left window, the live view.** Leave this running for the whole video:
  ```bash
  limactl shell pi -- watch -n1 pipeline status
  ```
- **Right window.** Type the commands below here.

## 0:00–0:30 Intro (say it while showing the status window)

> "This is my pipeline running on an ARM64 Ubuntu VM limited to 4 cores and 2 GB, like a Raspberry Pi 4. The collector saves every sample to SQLite with fsync. A separate sync process streams the data to Supabase, where each second is processed. The status line shows the mode, samples saved, samples in the cloud and the backlog."

Show that it's ARM64 with 4 cores and 2 GB:
```bash
limactl shell pi -- bash -c 'uname -m; nproc; free -m | head -2'
```

## 0:30–1:45 Network cut

Cut the network:
```bash
limactl shell pi -- sudo iptables -I OUTPUT -p tcp --dport 443 -j DROP
```

Point at the left window:
- the mode changes to **offline** within about 7 s;
- `samples_saved` keeps rising, `samples_in_cloud` stops, and the backlog grows;
- `offline_results` rises, because the device is now processing the windows itself.

Wait about 30 s, then restore the network:
```bash
limactl shell pi -- sudo iptables -D OUTPUT -p tcp --dport 443 -j DROP
```

Point at the left window: the mode goes back to **online** within about 3 s, and the backlog drains to near 0 within seconds.

## 1:45–2:30 Process kill

Kill both processes:
```bash
limactl shell pi -- sudo systemctl kill -s KILL pipeline-collect pipeline-sync
```

Show that systemd has restarted them:
```bash
limactl shell pi -- systemctl status pipeline-collect --no-pager | head -5
```

Point at the left window: `samples_saved` carries on, and `losses` does not change. The mock keeps 10 s of data, so the collector re-reads exactly what it missed.

## 2:30–4:00 Power cut

Note the session ID in the left window first. The left window will disconnect during this step.

Pull the power (a forced stop of the VM):
```bash
limactl stop --force pi
```

Power it back on:
```bash
limactl start pi
```

Restart the left window's live view:
```bash
limactl shell pi -- watch -n1 pipeline status
```

Show that there is a new session. The mock also lost power, so the old session's unread tail is listed as lost, not silently missing:
```bash
limactl shell pi -- pipeline sessions
```
```bash
limactl shell pi -- pipeline export --session <OLD_ID> --from device | tail -3
```

Say: "At most about 1 second of data can be lost on a power cut. Each reply is committed with fsync before the next one is requested."

## 4:00–4:45 Cloud copy is complete, and the results

Check that the cloud copy matches the device copy:
```bash
limactl shell pi -- sudo bash /opt/pipeline/scripts/failure_tests.sh verify
```

Show the latest window results (heart rate, `mode` online or offline):
```bash
limactl shell pi -- pipeline results | tail -3
```

## 4:45–5:00 Close

> "Every sample is either on disk exactly once or listed as lost with a reason. The cloud ends up with an identical copy. The test logs and the 1-hour resource figures are in the evidence folder."
