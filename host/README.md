# Host config (not managed by Argo CD)

Files here are installed by hand on the node and kept in git as the source of truth.

| File | Installed at |
|---|---|
| `k3s/config.yaml.example` | `/etc/rancher/k3s/config.yaml` (fill in your Tailscale IP) |
| `systemd/k3s.service.d/10-wait-for-tailscale.conf` | `/etc/systemd/system/k3s.service.d/` |

Install / update the systemd drop-in:
```sh
sudo install -D -m 644 host/systemd/k3s.service.d/10-wait-for-tailscale.conf \
  /etc/systemd/system/k3s.service.d/10-wait-for-tailscale.conf
sudo systemctl daemon-reload
```

Why the drop-in exists: k3s binds to the Tailscale IP, but `tailscaled` reports
"started" a few seconds before `tailscale0` actually has that IP. Without the
wait, k3s can start too early after a reboot and fail. See
[docs/lessons-learned.md](../docs/lessons-learned.md).
