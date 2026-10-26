# Lessons learned

Real problems from running this cluster, with the root cause and the fix.

## Traefik stuck after a chart upgrade

**Symptom.** The `helm-install-traefik` job crashlooped with `can't index item of type bool`. The Helm release was stuck at revision 1, and several hand-edited Deployment revisions were fighting over host ports, one of them Pending forever.

**Cause.** An old override set `ports.web.expose: true`. The bundled Traefik chart had since changed `expose` to a map (`expose: {default: true}`). The override also turned on `hostNetwork`, so Traefik and k3s's ServiceLB both wanted ports 80 and 443.

**Fix.** Deleted the override and started over with a minimal `HelmChartConfig` (HTTP→HTTPS redirect, access logs), managed by Argo CD. hostNetwork isn't needed: ServiceLB already binds 80/443 on the node and forwards them to Traefik's Service.

**Takeaway.** Hand-editing resources a controller owns creates state that can't be reproduced. If it isn't in git, it will drift.

## k3s racing Tailscale at boot

**Symptom.** After a reboot, k3s could fail to start, because it binds to the node's Tailscale IP.

**Cause.** `After=tailscaled.service` isn't enough. systemd treats tailscaled as started a few seconds *before* the `tailscale0` interface gets its IP.

**Fix.** A systemd drop-in (`host/systemd/k3s.service.d/`) that orders k3s after tailscaled *and* waits, for up to 60 s, until `tailscale0` has a `100.x` address. A reboot test showed tailscaled up at +4 s, k3s at +21 s with no restarts, and all apps healthy within about 2 minutes.

**Takeaway.** "Service started" and "service ready" are different things. Test reboots on purpose, before a power cut tests them for you.

## Apps exposed around the ingress

**Symptom.** Jellyfin was reachable over plain HTTP on `:8096`, bypassing TLS.

**Cause.** Its Service was `type: LoadBalancer`. With k3s ServiceLB, that opens the port on the node.

**Fix.** Every app Service is `ClusterIP`, and the only way in is an Ingress through Traefik on `websecure`. That's now a repo convention.

## A data disk that can block booting

**Cause.** The media disk was in `/etc/fstab` with default options. If that disk fails, systemd waits for it and drops into emergency mode, and the whole cluster stays down because of a non-essential disk.

**Fix.** Mount it with `nofail,x-systemd.device-timeout=10s`. The node still boots, and only the pods that need the media path fail.

## A credential pasted into a chat

**What happened.** While setting up backups, an S3 secret key ended up in a chat transcript.

**Fix.** Rolled the key immediately and confirmed the old one was rejected. Secrets now go only through `scripts/seal-secrets.sh`, which reads them with a hidden prompt and pipes them straight into `kubeseal`, so the plaintext never touches disk or history.

**Takeaway.** Assume a leaked secret is compromised. Rotate it first, then work out how it leaked.

## The first restore drill

**Why.** Backups had reported `Completed` every night, but that only proves the upload worked. Nobody had shown the data could come back.

**What I did.** I restored Portainer from last night's Velero backup into a scratch namespace (`namespaceMapping: {portainer: portainer-restore-test}`), next to the live app. All volume bytes came back. The restored Portainer reported that its admin user existed (`/api/users/admin/check` → 204), which proves it was the real data and not a fresh install. Then I deleted the scratch namespace.

**Gotchas.**
- **Exclude Ingresses and cert-manager objects** (`excludedResources`) when restoring next to a live app. Otherwise the copy claims the same hostname, and cert-manager requests a duplicate certificate.
- **Argo CD leaves the copies alone.** Its annotation-based resource tracking includes the namespace, so objects restored under a different namespace don't match any Application and aren't pruned or "healed".
- Velero logged one harmless warning (`Failed to get nodeOS for node ''`). It looks up the pod's node before the pod has been scheduled.

**Takeaway.** A backup you haven't restored is a hope, not a backup. Run the drill before you need it, and repeat it after big changes.
