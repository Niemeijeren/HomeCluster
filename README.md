# HomeCluster

A single-node Kubernetes homelab, run entirely through GitOps. One small PC (an HP ProDesk mini) serves media, a placeholder website and its own GitOps controller, and reports its own health to a dead-man's switch. Everything is reachable only over a private Tailscale network, but still gets real, browser-trusted TLS certificates.

This is the public, sanitized version of my private homelab repo. It has the same manifests and structure, with personal values swapped for placeholders, so it doubles as a template for your own setup.

> Deliberately overengineered: the point is to learn production patterns (GitOps, sealed secrets, offsite backups, pinned versions, default-deny networking, tenant isolation) on hardware that fits on a bookshelf.

## Stack

| Layer | Choice | Why |
|---|---|---|
| OS | Ubuntu Server LTS | Boring, long support, unattended security upgrades |
| Kubernetes | [k3s](https://k3s.io) | Single binary, tiny footprint, bundles Traefik, ServiceLB and local-path storage |
| Network access | [Tailscale](https://tailscale.com) | Zero open ports on the router. The node registers with its tailnet IP. |
| GitOps | [Argo CD](https://argo-cd.readthedocs.io), app-of-apps | Git is the single source of truth, and drift is reverted automatically (`selfHeal`) |
| Ingress | Traefik (bundled with k3s) | Configured through a `HelmChartConfig` that Argo CD owns |
| TLS | [cert-manager](https://cert-manager.io) + Let's Encrypt DNS-01 (Cloudflare) | Real certs for hosts that are never reachable from the internet |
| Secrets | [Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) | Encrypted secrets can live in a public repo |
| Backups | [Velero](https://velero.io) + [Kopia](https://kopia.io) to Cloudflare R2 | Encrypted, deduplicated, offsite. Volumes and host data handled separately. |
| Isolation | NetworkPolicies, Pod Security Admission, AppProject + ValidatingAdmissionPolicy | Default-deny per namespace; the website is a tenant that can't loosen its own limits |
| Alerting | [healthchecks.io](https://healthchecks.io) dead-man's switch | Alerts on failed checks *and* when the server goes silent |
| CI | GitHub Actions + [kubeconform](https://github.com/yannh/kubeconform) + a policy script | Every push is schema-validated (CRDs and Helm charts included) and checked against the repo conventions |
| Apps | Jellyfin, website placeholder | Media server, and a slot for a personal site |

## Architecture

A map of every part of the system, what it does, and how the parts talk to each other. Diagrams can be zoomed and panned on GitHub; click a ▶ section to expand it.

### The big picture

```mermaid
flowchart LR
  subgraph devices["Your devices (tailnet)"]
    dev["Laptop / phone"]
  end

  subgraph ext["External services"]
    gh["GitHub<br/>this repo"]
    dns["Cloudflare DNS<br/>*.example.com → Tailscale IP"]
    r2[("Cloudflare R2<br/>homecluster-backups")]
    le["Let's Encrypt"]
    hc["healthchecks.io"]
  end

  subgraph lan["Home LAN"]
    tv["TV<br/>(no Tailscale client)"]
  end

  subgraph host["Mini PC · Ubuntu host"]
    ts["tailscaled<br/>100.x.y.z"]
    smb["Samba share"]
    media[("1.8T disk<br/>/srv/media")]

    subgraph k3s["k3s · single-node Kubernetes"]
      slb["ServiceLB<br/>:80 / :443"]
      traefik["Traefik<br/>routing + TLS"]

      subgraph platform["Platform"]
        argocd["Argo CD"]
        cm["cert-manager"]
        ss["Sealed Secrets"]
        velero["Velero<br/>+ node-agent"]
        kopia["Kopia CronJob"]
        mon["cluster-checks<br/>hourly CronJob"]
      end

      subgraph apps["Apps"]
        jf["Jellyfin"]
        lt["website<br/>placeholder"]
      end

      pvc[("local-path PVCs<br/>476G system disk")]
    end
  end

  dev -- "1 · DNS lookup" --> dns
  dev -- "2 · HTTPS over WireGuard" --> ts
  ts --> slb --> traefik
  traefik --> jf
  traefik --> lt
  traefik --> argocd
  dev -- "SMB file copy" --> smb --> media
  tv -. "optional: HTTP :8096<br/>source range = TV only" .-> jf
  jf -. "reads movies + TV (read-only)" .-> media
  jf --- pvc

  argocd -- "pulls desired state" --> gh
  argocd -- "applies YAML" --> apps
  argocd -- "applies YAML" --> platform
  cm -- "DNS-01 TXT record" --> dns
  cm -- "ACME order" --> le
  cm -- "TLS certificates" --> traefik
  ss -- "decrypted Secrets" --> apps
  velero -- "reads" --> pvc
  velero -- "02:00 encrypted" --> r2
  kopia -- "reads photos + docs" --> media
  kopia -- "03:00 encrypted" --> r2
  mon -- "hourly ping per check" --> hc
```

<details>
<summary><b>Host layer</b>: the ProDesk and what runs outside Kubernetes</summary>

| Part | Role |
|---|---|
| HP ProDesk | 20 cores, 30 GiB RAM, Ubuntu 26.04. The whole cluster is this one machine. |
| 476G NVMe | OS, k3s and every app volume (`/var/lib/rancher/k3s/storage`). |
| 1.8T NVMe | Media (`MEDIA_PATH`). Mounted with `nofail`, so the server still boots if this disk dies. |
| tailscaled | Joins the server to the tailnet. k3s waits for it at boot, because the node IP *is* the Tailscale IP. |
| Samba | Shares the media folder with your other machines (over Tailscale). You drop files in, Jellyfin reads the same folder. |
| ufw, unattended-upgrades | Firewall (nothing allowed from the LAN) and automatic security patches. Note: Kubernetes hostPort and NodePort traffic is DNATed *before* ufw sees it, so LAN exposure of k8s Services is controlled in Kubernetes (Service source ranges), not in ufw. |
| Docker | Installed but disabled. Kept for building images. k3s doesn't use it (it has its own containerd). |

**Why run Samba on the host, not in Kubernetes?** It's a plain file share for the LAN. Containerising it adds complexity without any benefit.
</details>

<details>
<summary><b>Network layer</b>: why the URLs are public but the services are private</summary>

- **Tailscale** is a WireGuard mesh VPN between your devices. Only devices logged into your tailnet can reach `100.x.y.z`. This is the main security boundary.
- **Cloudflare DNS** publishes `*.example.com → 100.x.y.z`. Anyone can *look up* the name, but the address is unreachable outside the tailnet.
- **Why do it this way?** You get real hostnames and real (trusted) HTTPS certificates, without opening a single port on the router.
- **One optional LAN exception** (off by default): a TV without a Tailscale client can play from Jellyfin over plain HTTP on the LAN. `jellyfin-lan` is a LoadBalancer Service whose `loadBalancerSourceRanges` is that one device's IP, with no NodePorts (source ranges don't apply to those). CI fails on any other LoadBalancer or NodePort Service, or on a different source range.
- The apex `example.com` is proxied by Cloudflare and doesn't reach the cluster yet. That's where the public website goes, via a Cloudflare Tunnel (see *Planned*).
</details>

<details>
<summary><b>Kubernetes layer</b>: what k3s bundles</summary>

| Part | Role |
|---|---|
| containerd | Runs the containers. |
| ServiceLB (klipper) | Binds :80/:443 on the node and forwards to Traefik. It replaces a cloud load balancer. |
| Traefik | Ingress controller. It routes by hostname, terminates TLS and redirects HTTP to HTTPS. |
| local-path | Storage: each PVC is a folder on the system disk. Simple, but tied to this node, and by default deleting a PVC deletes its data. That's why every PVC has `Prune=false,Delete=false` and new ones use the `local-path-retain` StorageClass. |
| kube-router (network policy) | Enforces NetworkPolicies. Every workload namespace is default-deny, ingress and egress. |
| Flannel, CoreDNS, metrics-server | Pod networking, in-cluster DNS, and CPU/memory stats. |
</details>

<details>
<summary><b>Platform layer</b>: the components that keep everything running</summary>

| Component | Role | Talks to |
|---|---|---|
| **Argo CD** | GitOps controller. It continuously makes the cluster match `main` of this repo, and undoes manual edits (`selfHeal`). | GitHub (read-only deploy key), the Kubernetes API |
| **cert-manager** | Requests and renews Let's Encrypt certificates. It proves domain ownership by creating a DNS TXT record (DNS-01), which works even though the services aren't publicly reachable. | Cloudflare API, Let's Encrypt, Traefik |
| **Sealed Secrets** | Lets secrets live in git. `kubeseal` encrypts with the controller's public key, and only the controller can decrypt. Its private key is backed up in the encrypted DMG (kept offline in an encrypted volume). | Argo CD, the apps |
| **Velero** | Backs up every app's volumes nightly and restores them, possibly into another namespace (restore tested 2026-10-26). | R2, the PVCs |
| **Kopia CronJob** | Encrypted, deduplicated backup of photos and documents. | Media disk, R2 |
| **cluster-checks** | Hourly health checks (backups, node, Argo CD, certificates, the HTTPS path), one ping per check to healthchecks.io. See [Monitoring](#monitoring). | Kubernetes API (read-only), Traefik, healthchecks.io |
</details>

<details>
<summary><b>Apps</b></summary>

| App | URL | Data |
|---|---|---|
| Jellyfin | jellyfin.example.com, plus optionally `http://<node LAN IP>:8096` for one LAN device | Config on a 10Gi PVC. Only movies and TV are mounted, read-only. Runs as a uid that exists nowhere on the host, with no capabilities and a read-only root filesystem. |
| website | site.example.com | None. A placeholder until the website repo ships an image. Deployed as a restricted tenant (see [Conventions](#conventions)). |
| Argo CD UI | argocd.example.com | None. |
</details>

### Flow 1: a request (opening Jellyfin)

```mermaid
sequenceDiagram
  autonumber
  participant B as Browser (on tailnet)
  participant D as Cloudflare DNS
  participant N as node port 443 (ServiceLB)
  participant T as Traefik
  participant J as Jellyfin pod
  B->>D: jellyfin.example.com?
  D-->>B: 100.x.y.z
  B->>N: HTTPS, carried over Tailscale
  N->>T: forward
  Note over T: TLS with the cert from cert-manager,<br/>pick a route by Host header
  T->>J: HTTP to Service jellyfin :8096
  J-->>B: page
```

### Flow 2: a change (GitOps)

```mermaid
sequenceDiagram
  autonumber
  actor You
  participant G as GitHub (this repo)
  participant A as Argo CD
  participant K as Kubernetes API
  You->>You: edit YAML, kubectl diff / dry-run
  You->>G: git push
  loop every ~3 min
    A->>G: fetch main
  end
  A->>A: compare git with the live cluster
  A->>K: apply the difference
  K-->>A: resources Healthy
  Note over A,K: Someone runs kubectl edit by hand?<br/>selfHeal reverts it.
```

<details>
<summary><b>Why GitOps?</b></summary>

- **History and undo:** every change is a commit, so `git log` is the audit trail and `git revert` is the rollback.
- **Rebuildable:** the cluster can be recreated from the repo, so cluster objects don't need backups.
- **No drift:** what's in git is what runs. Nobody's "quick fix" survives unrecorded.
- **App of apps:** the `root` Application syncs `argocd/apps/`, which holds one Application per component. Sync waves order them: controllers first, then config, then apps.
</details>

### Flow 3: a secret

```mermaid
sequenceDiagram
  autonumber
  actor You
  participant KS as kubeseal (CLI)
  participant G as GitHub
  participant A as Argo CD
  participant SS as Sealed Secrets controller
  participant P as App pod
  You->>KS: plain Secret (never committed)
  KS->>KS: encrypt with the controller's public key
  KS->>G: SealedSecret YAML (safe to commit)
  A->>G: sync
  A->>SS: create the SealedSecret
  SS->>SS: decrypt with the private key
  SS->>P: normal Secret, mounted or as env vars
```

### Flow 4: nightly backups

```mermaid
sequenceDiagram
  participant V as Velero + node-agent
  participant K as Kopia CronJob
  participant R as Cloudflare R2
  Note over V: 02:00 UTC
  V->>V: Kubernetes objects + PVC files of all app namespaces
  V->>R: encrypted upload to velero/ (kept 30 days)
  Note over K: 03:00 UTC
  K->>K: snapshot of media/photos + media/documents
  K->>R: encrypted, deduplicated upload to kopia/media/
```

### Flow 5: disaster recovery (server lost)

```mermaid
flowchart TB
  a["Install Ubuntu, Tailscale, k3s"] --> b["Restore the sealing key<br/>from its offline backup"]
  b --> c["Bootstrap Argo CD<br/>(see Run it yourself)"]
  c --> d["Argo CD rebuilds every<br/>app and component from git"]
  d --> e["Velero restores app data<br/>from R2"]
  d --> f["Kopia restores photos<br/>and documents"]
  e --> g["Back in business"]
  f --> g
```

Not recoverable by design: movies and TV, which can be fetched again.

### Planned: public website

```mermaid
flowchart LR
  user["Visitor"] --> cf["Cloudflare edge"]
  cf --> acc["Cloudflare Access<br/>email login for /private/*"]
  acc --> tun["Cloudflare Tunnel"]
  tun --> cfd["cloudflared pod<br/>(outbound only)"]
  cfd --> site["Astro website"]
  site --> api["read-only cluster<br/>status API"]
  site --> pg[("Postgres<br/>CloudNativePG")]
  pg -- "WAL + base backups" --> r2[("R2")]
  ci["GitHub Actions<br/>(website repo)"] -- "image" --> ghcr["GHCR"]
  ci -- "bump tag" --> gh["this repo"]
  gh --> argo["Argo CD"] --> site
```

**How a request flows:** the device resolves `jellyfin.example.com` through public DNS to the node's Tailscale IP (`100.x.y.z`). Only devices on the tailnet can route to that address. The connection reaches k3s ServiceLB on :443, then the Traefik Service, which terminates TLS with a cert-manager certificate. The Ingress host rule picks the app's ClusterIP Service, which forwards to the pod.

Public DNS pointing at a private IP is intentional: names and certificates work everywhere, but the services are invisible from the internet. See [docs/design-decisions.md](docs/design-decisions.md).

## Repository layout

```
bootstrap/            applied by hand, once, to start Argo CD
  argocd-values.yaml    Argo CD Helm values (after bootstrap, Argo CD uses them to manage itself)
  root-app.yaml         the "app of apps": points Argo CD at argocd/apps/
argocd/apps/          one Argo CD Application per component
infrastructure/       cluster-wide config: ClusterIssuer, Traefik overrides, Velero schedule,
                      StorageClass, and the platform-owned boundary around each tenant
apps/                 workloads, one kustomize folder per app
host/                 node-level config Argo CD can't manage (k3s config, systemd drop-in)
scripts/              configure.sh (fill in your values), seal-secrets.sh / seal-from-prompt.sh
                      (create secrets), check-policy.py + render-charts.py (CI checks)
docs/                 design decisions and lessons learned
```

Sync order is controlled with `argocd.argoproj.io/sync-wave`, so controllers and CRDs exist before anything uses them:

| wave | what |
|---|---|
| -3 | `cert-manager`, `sealed-secrets`, `velero` (controllers and CRDs), AppProject `website` |
| -2 | `argocd` (manages its own upgrades) |
| -1 | `infra-cert-manager` (ClusterIssuer), `infra-traefik` (HelmChartConfig), `infra-velero` (Schedule, credentials), `infra-storage` (StorageClass), `infra-tenants` (website's Namespace, NetworkPolicies, admission policy) |
| 0 | `jellyfin`, `media-backup`, `monitoring`, `website` |

Every Application retries a failed sync with backoff (`retry.refresh: true`), so a cold rebuild where a CRD isn't there yet recovers on its own instead of waiting for the next commit.

## Run it yourself

You need a Linux box, a domain on Cloudflare, a Tailscale account, an S3-compatible bucket (the R2 free tier is enough to start) and a free healthchecks.io account.

**1. Fork and configure**
```sh
git clone https://github.com/<you>/HomeCluster.git && cd HomeCluster
cp config.env.example config.env    # domain, email, repo URL, media path, bucket, IPs
./scripts/configure.sh              # writes your values into the manifests
git diff                            # review, then commit
```

**2. Prepare the host**
```sh
curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up
sudo install -D -m 644 host/k3s/config.yaml.example /etc/rancher/k3s/config.yaml
sudoedit /etc/rancher/k3s/config.yaml   # set node-ip to `tailscale ip -4`
sudo install -D -m 644 host/systemd/k3s.service.d/10-wait-for-tailscale.conf \
  /etc/systemd/system/k3s.service.d/10-wait-for-tailscale.conf
curl -sfL https://get.k3s.io | sh -
```
Now that the node has its Tailscale IP, set `NODE_IP` in `config.env` to the same address, run `./scripts/configure.sh` again and commit. The monitoring job reaches the API server at that IP.

Optional: to let one LAN device without Tailscale (a TV) reach Jellyfin, set `LAN_CLIENT_IP`, re-run `configure.sh` and uncomment `service-lan.yaml` in `apps/jellyfin/kustomization.yaml`. It's plain HTTP, so give that device its own non-admin Jellyfin user.

In Cloudflare DNS, add an A record `*` (DNS only, not proxied) pointing to the node's Tailscale IP.

**3. Bootstrap Argo CD**
```sh
helm repo add argo https://argoproj.github.io/argo-helm
helm install argocd argo/argo-cd --version 10.10.0 -n argocd --create-namespace \
  -f bootstrap/argocd-values.yaml
kubectl apply -f bootstrap/root-app.yaml
```
Argo CD now installs everything else from `argocd/apps/`, including the Sealed Secrets controller. The apps that need secrets stay degraded until step 4.

If your fork is private, give Argo CD a read-only deploy key first:
```sh
kubectl -n argocd create secret generic repo \
  --from-literal=type=git --from-literal=url=git@github.com:<you>/HomeCluster.git \
  --from-file=sshPrivateKey=<read-only-deploy-key>
kubectl -n argocd label secret repo argocd.argoproj.io/secret-type=repository
```
Then use the `git@github.com:` URL as `REPO_URL`.

**4. Seal your secrets and push**
```sh
./scripts/seal-secrets.sh   # prompts for the Cloudflare token, S3 keys and ping key; generates backup passwords
git add -A && git commit -m "Add sealed secrets" && git push
```

**5. Log in** at `https://argocd.<your domain>` as `admin`:
`kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d`. Change the password, then delete that secret.

**6. Arm the alerts** in healthchecks.io after the first `cluster-checks` run (see [Monitoring](#monitoring)).

## Secrets

No plain `Secret` is ever committed, and CI fails if one is. Secrets are encrypted with the cluster's Sealed Secrets public key, and only the controller inside the cluster can decrypt them. The `*.sealed.yaml` files in this repo are placeholders until you run `scripts/seal-secrets.sh`.

**Back up the sealing key**, or your sealed secrets are unreadable after a rebuild:
```sh
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml > sealing-key.yaml
```
Keep that file in a password manager or an encrypted volume, never in git.

## Monitoring

Alerting is a dead-man's switch on [healthchecks.io](https://healthchecks.io) (free tier). The CronJob `monitoring/cluster-checks` runs `apps/monitoring/check.py` every hour at :17 and pings one check per signal, with the reasons in the body when something's wrong:

| check | fails when |
|---|---|
| `homecluster-velero` | newest finished daily backup not `Completed` or older than 26h; backup location not `Available` |
| `homecluster-kopia` | `media-backup` not successful in 26h (expected for a few nights during a big first upload) |
| `homecluster-node` | node not Ready, or Disk/Memory/PID pressure |
| `homecluster-argocd` | an app Degraded/Missing/Unknown, Progressing > 2h, last sync Failed, or a sync Running > 1h |
| `homecluster-certs` | a certificate not Ready or valid < 14 days |
| `homecluster-ingress` | HTTPS through Traefik (real SNI/Host, certificate verified) to `jellyfin.example.com/health` or `site.example.com/` isn't 200. Catches what pods and Argo CD can't see: a NetworkPolicy, Ingress or certificate breaking the user's path |

One check per signal, because healthchecks.io only notifies on a state change: a check that's already red would swallow any new failure it also covers. If the server, k3s or the job itself is down, all pings stop and healthchecks.io alerts after the grace time. An alert sent from inside the cluster can never cover that case.

The job is built to be boring to trust: stdlib-only Python in an image pinned by digest, a read-only ServiceAccount (no Secrets, namespaced Roles where possible), a namespace with Pod Security `restricted` enforced, and egress limited to DNS, the API server, Traefik and the public internet.

**Not armed until set up.** A check that has never been pinged can't go down, and auto-created checks start with the project defaults. After the first run, in the healthchecks.io UI: all six checks exist, each has period 1 hour and grace 1 hour, and the email integration is on. Then drill it: set one check to 1 min/1 min, wait for the email, set it back. Turn on 2FA, and use a project only for this server.

The ping key is the SealedSecret `monitoring/healthchecks`. Anyone with it can send "all good", so rotate it in healthchecks.io if it leaks and re-seal with `scripts/seal-from-prompt.sh monitoring healthchecks ping-key apps/monitoring/healthchecks.sealed.yaml`. Not covered, so put it on the calendar: the Tailscale node key expiry.

Test the checks by hand: `kubectl -n monitoring create job --from=cronjob/cluster-checks check-now` and `kubectl -n monitoring logs job/check-now`.

## Backups

| what | how | when (UTC) | bucket prefix | kept |
|---|---|---|---|---|
| PVC contents of every app namespace | Velero file-system backup (Kopia via node-agent) | 02:00 | `velero/` | 30 days |
| `<media>/photos`, `<media>/documents` | Kopia CronJob `media-backup` | 03:00 | `kopia/media/` | 10 latest, 14 daily, 8 weekly, 12 monthly, 3 yearly |

Not backed up, on purpose:
- **Cluster objects:** git plus Argo CD rebuild them.
- **Movies and TV:** large, and can be fetched again.
- **Secrets:** excluded from Velero, because its metadata tarballs aren't encrypted client-side. The SealedSecrets are in git.

Both Kopia repositories are encrypted with their own password. Velero backs up every pod volume by default, and pods opt out with `backup.velero.io/backup-volumes-excludes` (Jellyfin excludes its cache, `/tmp` and media mounts).

```sh
kubectl -n velero get backupstoragelocation    # PHASE must be Available
kubectl -n velero get backups
kubectl -n media-backup create job --from=cronjob/media-backup media-backup-manual
```

The backup config targets Cloudflare R2 (`region: auto`). For AWS S3, B2 or MinIO, set the endpoint in `config.env` and adjust `region` in `argocd/apps/velero.yaml`.

### Restoring an app

Argo CD's selfHeal would recreate an empty app while Velero restores it, so take the app out of automated sync first:

1. **Restore mode:** in `argocd/apps/<app>.yaml`, delete the `automated:` block (keep `retry`/`syncOptions`), commit and push. Wait until root has applied it: `kubectl -n argocd get app <app> -o jsonpath='{.spec.syncPolicy.automated}'` prints nothing.
2. Delete the namespace, after checking it's the right one (a typo like `argocd` or `velero` is a self-inflicted outage). You have to type the name again; anything else aborts:
   ```sh
   NS=<app>; kubectl get ns $NS && read -rp "type $NS: " a
   [ "$a" = "$NS" ] && kubectl delete namespace $NS
   ```
   With `Retain` PVs, the old data stays on disk (PV `Released`) as a last resort.
3. Restore a backup **by name**, picked from `kubectl -n velero get backups` (check it's `Completed` and from before the data loss; a 02:00 run during the restore makes an empty "latest"):
   ```sh
   kubectl -n velero create -f - <<<'{"apiVersion":"velero.io/v1","kind":"Restore","metadata":{"generateName":"restore-<app>-","namespace":"velero"},"spec":{"backupName":"<daily-...>","includedNamespaces":["<app>"]}}'
   ```
4. Wait for `kubectl -n velero get restore` to show `Completed` and every `kubectl -n velero get podvolumerestores` to show `Completed`. Check the app works.
5. **Check Retain.** The PVC gets a freshly provisioned PV: `kubectl get pv`, and if a new PV says `Delete`, `kubectl patch pv <new-pv> -p '{"spec":{"persistentVolumeReclaimPolicy":"Retain"}}'`.
6. `git revert` the restore-mode commit and push. Argo CD adopts the restored objects (they keep their tracking-id annotation) and shows Synced.
7. The restored objects are as they were at backup time. If they carry fields that aren't in git (drift), Argo CD won't remove them: recreate the Deployment with `kubectl -n <app> delete deploy <name> --cascade=foreground`.
8. Once you're happy, delete the old `Released` PV and its folder under `/var/lib/rancher/k3s/storage`.

**Scratch restores** (into a `namespaceMapping` namespace, for testing): the restored NetworkPolicies come along and deny everything else, so check the copy with `kubectl port-forward` (via the kubelet, not affected), not with a curl pod. Before deleting the scratch namespace, set its PV to `Delete` so the provisioner removes the folder too.

Now and then (and after every restore), run `kubectl get pv | grep Released`: Retain means deleted data stays on disk until someone removes it by hand.

The media Kopia repository can be restored from any machine with `kopia repository connect s3 --bucket=<bucket> --prefix=kopia/media/ --endpoint=<endpoint> --region=auto` followed by `kopia snapshot restore`.

## Conventions

CI enforces most of these (`scripts/check-policy.py`).

- **Pin every image and chart version.** Upgrades are commits, never `:latest` or a manual `helm upgrade`.
- **Stateful single-instance apps use `strategy: Recreate`**, because a RollingUpdate would run two pods against one ReadWriteOnce volume.
- **Every PVC is annotated `Prune=false,Delete=false`** and new ones use `storageClassName: local-path-retain`, so neither Argo CD nor a namespace delete can destroy the data. For personal data, Retain also means a delete leaves it on disk: purge the Released PV deliberately.
- **Default-deny networking.** Every namespace that runs workloads has a `default-deny` NetworkPolicy (ingress and egress) plus explicit allows in `networkpolicy.yaml`. Internet egress uses an `ipBlock` that excludes the LAN, the tailnet (and with it the node), pod/service ranges and link-local. kubelet probes aren't affected, and the API server is reached at the node's address (`NODE_IP`:6443), because the ClusterIP is DNATed before the policy check.
- **NetworkPolicy emergencies:** `kubectl delete netpol` doesn't stick (selfHeal recreates it). Either `git revert` the commit, or for an immediate fix disable automated sync on that one Application, delete the policy, and re-enable sync after the fix is in git.
- **Tenants.** `apps/website` is deployed through AppProject `website`: only namespace `website`, Deployment/Service/ConfigMap/Ingress/SealedSecret, nothing cluster-scoped, and `FailOnSharedResource` so it can't adopt another app's objects. Its boundary is owned by the platform in `infrastructure/tenants/website/` (app `infra-tenants`, project `default`): the Namespace with Pod Security `restricted`, its NetworkPolicies, and a ValidatingAdmissionPolicy (only hosts `example.com`/`site.example.com`, only ClusterIP Services without externalIPs). The tenant can't loosen any of it. What it doesn't stop: anyone who can push to this repo can edit `argocd/apps/` (cluster-admin through root), so the website's CI must never hold a write key to this repo.
- **Services are ClusterIP.** The only way in is Traefik, through an Ingress with TLS. One documented, optional exception: `jellyfin/jellyfin-lan` (see the network layer above). Any other LoadBalancer or NodePort Service fails CI.
- **No host access.** No `hostPort`, `hostNetwork`, `hostPID` or `hostIPC`: they bypass Services, source ranges and the firewall.
- **Never `kubectl patch`/`edit` an Argo-managed object.** Argo CD's apply doesn't remove fields it didn't set, so an added field (say, a `hostPort`) stays live and invisible while the app shows Synced. Change git instead.
- **Every workload has probes and resource requests/limits.**

## Roadmap

- [x] Restore drill: restored an app into a scratch namespace from R2 (see lessons learned)
- [x] NetworkPolicies (default-deny per namespace) and restricted `securityContext`s
- [x] Tenant isolation for the website: AppProject, Pod Security, admission policy
- [x] Alerting: healthchecks.io dead-man's switch
- [x] CI: repo conventions and Helm chart rendering, not just schemas
- [ ] Public website through a Cloudflare Tunnel, with Cloudflare Access in front of private pages
- [ ] PostgreSQL via CloudNativePG, with WAL archiving to R2
- [ ] CI in the website repo: build to GHCR, bump the image tag here
- [ ] Intel iGPU (`/dev/dri`) for Jellyfin hardware transcoding
- [ ] Wildcard certificate instead of one per host
- [ ] Monitoring: kube-prometheus-stack + Grafana

## Further reading

- [docs/design-decisions.md](docs/design-decisions.md): why each piece is built the way it is
- [docs/lessons-learned.md](docs/lessons-learned.md): things that broke, and what fixed them
