# Design decisions

Short records of the choices behind this cluster: the context, what was decided, and the trade-off accepted.

## 1. GitOps with Argo CD, app-of-apps

**Context.** The cluster started out configured by hand: `helm install`, `kubectl edit`, manifests in a home directory. After a while nobody (me included) could say what was actually running or why.

**Decision.** Git is the only source of truth. One `root` Application, applied once by hand, watches `argocd/apps/`. Each file there is an Application for one component. Every Application has `automated` sync with `selfHeal: true`.

**Trade-off.** A quick `kubectl edit` gets reverted within minutes, so every change becomes a commit. That's the point: history, review and rollback come for free. Argo CD also manages its own Helm release (wave -2), with `prune: false` so a bad commit can't delete the controller that would fix it.

## 2. Private network, public DNS, real certificates

**Context.** I wanted HTTPS with browser-trusted certs, but no services exposed to the internet and no ports forwarded on the router.

**Decision.**
- The node joins a Tailscale tailnet, and k3s uses the Tailscale IP as its `node-ip`.
- A public wildcard DNS record `*.example.com` points at that `100.x.y.z` address. It resolves everywhere, but only tailnet members can route to it.
- cert-manager gets Let's Encrypt certificates through the **DNS-01** challenge (a TXT record created via the Cloudflare API). HTTP-01 would need the host reachable from the internet; DNS-01 doesn't.

**Trade-off.** The hostnames are public knowledge, and certificate transparency logs list them anyway. Reachability is what's protected, not obscurity. k3s now depends on Tailscale at boot (see lessons learned).

## 3. Sealed Secrets over SOPS or an external vault

**Decision.** Bitnami Sealed Secrets. Secrets are encrypted with the controller's public key and committed. Only the in-cluster controller can decrypt them.

**Why.** It has no external dependency (no cloud KMS, no Vault to run) and fits the "everything in git" model. The cost is that the controller's private key becomes a critical asset: it's exported once and kept offline. Losing it means re-sealing every secret.

## 4. Use k3s's bundled components

**Decision.** Keep k3s's Traefik, ServiceLB (klipper) and local-path provisioner instead of swapping in ingress-nginx, MetalLB and Longhorn.

**Why.** On a single node, MetalLB and Longhorn add moving parts without adding resilience. Traefik is customised through a `HelmChartConfig` that Argo CD owns, so its config is in git like everything else.

**Trade-off.** local-path volumes live on one disk and have `reclaimPolicy: Delete`. That's why every PVC carries `Prune=false,Delete=false` for Argo CD, and why backups matter.

## 5. Backups: two tools for two kinds of data

**Context.** There are two kinds of irreplaceable data: app state in PVCs (Jellyfin's library database, Portainer's settings), and personal files on a host disk (photos, documents). Velero only backs up volumes that belong to a PVC, so it can't see a `hostPath`.

**Decision.**
- **Velero** with file-system backup (Kopia uploader, node-agent DaemonSet) for PVCs. local-path has no CSI snapshot support, so file-system backup is the only option. It's opt-out per volume, so a new app is covered automatically.
- **A Kopia CronJob** for the photo and document folders, mounted read-only.
- Both go to Cloudflare R2 (S3-compatible, no egress fees), each in its own encrypted repository, using an API token scoped to that one bucket.
- **Not backed up:** cluster objects (rebuilt from git), Secrets (Velero's metadata tarballs aren't encrypted client-side), movies and TV (replaceable, and large).

**Trade-off.** Two tools to monitor instead of one, but each one does the job it's good at.

## 6. Pin everything

Every image tag and chart version is pinned, and images that ran as `:latest` were pinned to the exact build that was already running. An upgrade is a one-line diff that can be reviewed and reverted. Nothing changes because a pod happened to restart.

## 7. Single node, on purpose

There's no HA: one machine, one disk for the OS and volumes, one for media. The design assumes the node *will* die at some point, and makes rebuilding cheap instead: reinstall k3s, bootstrap Argo CD, restore the sealing key, restore volumes from R2.
