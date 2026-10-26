#!/usr/bin/env python3
"""Hourly health checks, reported to healthchecks.io (a dead-man's switch).

Each check pings its own slug: success with "ok", or /fail with the reasons
in the body (healthchecks.io puts them in the alert). One slug per signal on
purpose: healthchecks.io only notifies on a state change, so a check that's
already red (say, Kopia during a multi-night first upload) would swallow a
new failure of anything else it covered. If this job stops
running (node down, k3s broken, job stuck), the pings stop and
healthchecks.io alerts on its own: the one failure an in-cluster alert can't
report. Standard library only; reads the Kubernetes API with the pod's
read-only ServiceAccount (see rbac.yaml).
"""
import datetime as dt
import json
import os
import socket
import ssl
import sys
import urllib.request

API = "https://kubernetes.default.svc"
SA = "/var/run/secrets/kubernetes.io/serviceaccount"
PING = "https://hc-ping.com/" + os.environ["HC_PING_KEY"]
NOW = dt.datetime.now(dt.timezone.utc)

# Daily jobs get 2h of slack on top of their 24h period.
MAX_BACKUP_AGE_H = 26
# cert-manager renews 30 days before expiry, so < 14 days means renewal is failing.
MIN_CERT_DAYS = 14
# "Busy" and "stuck" look the same without a time limit.
MAX_PROGRESSING_H = 2
MAX_SYNC_RUNNING_H = 1
# The user's path: HTTPS through Traefik, with SNI/Host like a browser. Pods can
# be Ready and Argo Healthy while a NetworkPolicy or Ingress breaks this.
TRAEFIK = "traefik.kube-system.svc.cluster.local"
INGRESS_PROBES = (("jellyfin.example.com", "/health"), ("site.example.com", "/"))
# Velero phases after which a backup won't change any more.
VELERO_FINAL = {"Completed", "PartiallyFailed", "Failed", "FailedValidation"}

CTX = ssl.create_default_context(cafile=f"{SA}/ca.crt")
with open(f"{SA}/token") as f:
    TOKEN = f.read().strip()


def get(path):
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {TOKEN}"})
    with urllib.request.urlopen(req, context=CTX, timeout=20) as r:
        return json.load(r)


def age_h(timestamp):
    return (NOW - dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))).total_seconds() / 3600


def check_velero():
    problems = []
    for bsl in get("/apis/velero.io/v1/namespaces/velero/backupstoragelocations")["items"]:
        phase = bsl.get("status", {}).get("phase")
        if phase != "Available":
            problems.append(f"BackupStorageLocation {bsl['metadata']['name']} is {phase}")

    backups = get("/apis/velero.io/v1/namespaces/velero/backups"
                  "?labelSelector=velero.io/schedule-name%3Ddaily")["items"]
    # Judge the newest finished backup; one stuck in progress shows up via its age.
    done = [b for b in backups if b.get("status", {}).get("phase") in VELERO_FINAL]
    if not done:
        problems.append("no finished daily backup")
        return problems
    newest = max(done, key=lambda b: b["metadata"]["creationTimestamp"])
    name, phase = newest["metadata"]["name"], newest["status"]["phase"]
    if phase != "Completed":
        problems.append(f"newest daily backup {name} is {phase}")
    age = age_h(newest["metadata"]["creationTimestamp"])
    if age > MAX_BACKUP_AGE_H:
        problems.append(f"newest finished daily backup {name} is {age:.0f}h old")
    return problems


def check_kopia():
    cj = get("/apis/batch/v1/namespaces/media-backup/cronjobs/media-backup")
    last = cj.get("status", {}).get("lastSuccessfulTime")
    if not last:
        return ["media-backup never succeeded"]
    if age_h(last) > MAX_BACKUP_AGE_H:
        return [f"media-backup last succeeded {age_h(last):.0f}h ago"]
    return []


def check_node():
    problems = []
    for node in get("/api/v1/nodes")["items"]:
        conditions = {c["type"]: c["status"] for c in node["status"]["conditions"]}
        name = node["metadata"]["name"]
        if conditions.get("Ready") != "True":
            problems.append(f"{name}: Ready={conditions.get('Ready')}")
        # A full disk evicts pods while the node often stays Ready.
        for pressure in ("DiskPressure", "MemoryPressure", "PIDPressure"):
            if conditions.get(pressure) == "True":
                problems.append(f"{name}: {pressure}")
    return problems


def check_argocd():
    problems = []
    for app in get("/apis/argoproj.io/v1alpha1/namespaces/argocd/applications")["items"]:
        status = app.get("status", {})
        name = app["metadata"]["name"]
        health = status.get("health", {})
        if health.get("status") in ("Degraded", "Missing", "Unknown"):
            problems.append(f"{name} health {health['status']}")
        elif health.get("status") == "Progressing" and health.get("lastTransitionTime") \
                and age_h(health["lastTransitionTime"]) > MAX_PROGRESSING_H:
            problems.append(f"{name} Progressing for {age_h(health['lastTransitionTime']):.0f}h")
        op = status.get("operationState", {})
        if op.get("phase") in ("Failed", "Error"):
            problems.append(f"{name} last sync {op['phase']}")
        elif op.get("phase") == "Running" and op.get("startedAt") \
                and age_h(op["startedAt"]) > MAX_SYNC_RUNNING_H:
            problems.append(f"{name} sync running for {age_h(op['startedAt']):.0f}h")
    return problems


def check_certs():
    problems = []
    for cert in get("/apis/cert-manager.io/v1/certificates")["items"]:
        name = f"{cert['metadata']['namespace']}/{cert['metadata']['name']}"
        status = cert.get("status", {})
        ready = {c["type"]: c["status"] for c in status.get("conditions", [])}.get("Ready")
        if ready != "True":
            problems.append(f"{name}: Ready={ready}")
        if status.get("notAfter") and -age_h(status["notAfter"]) / 24 < MIN_CERT_DAYS:
            problems.append(f"{name}: expires {status['notAfter']}")
    return problems


def check_ingress():
    problems = []
    # System CAs: the certificates are Let's Encrypt, so this also checks them.
    tls = ssl.create_default_context()
    for host, path in INGRESS_PROBES:
        try:
            with socket.create_connection((TRAEFIK, 443), timeout=10) as raw, \
                    tls.wrap_socket(raw, server_hostname=host) as conn:
                conn.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                status_line = conn.recv(256).split(b"\r\n", 1)[0].decode(errors="replace")
            if status_line.split(" ")[1:2] != ["200"]:
                problems.append(f"https://{host}{path}: {status_line}")
        except Exception as e:
            problems.append(f"https://{host}{path}: {e!r}")
    return problems


CHECKS = (
    ("homecluster-velero", check_velero),
    ("homecluster-kopia", check_kopia),
    ("homecluster-node", check_node),
    ("homecluster-argocd", check_argocd),
    ("homecluster-certs", check_certs),
    ("homecluster-ingress", check_ingress),
)


def ping(slug, problems):
    url = f"{PING}/{slug}{'/fail' if problems else ''}?create=1"
    body = "\n".join(problems) or "ok"
    urllib.request.urlopen(urllib.request.Request(url, data=body.encode()), timeout=20)


def main():
    exit_code = 0
    for slug, check in CHECKS:
        try:
            problems = check()
        except Exception as e:  # report a broken check instead of going silent
            problems = [f"check crashed: {e!r}"]
        print(f"{slug}: {'; '.join(problems) or 'ok'}")
        try:
            ping(slug, problems)
        except Exception as e:
            # A missed ping alerts anyway; fail the Job so it shows in kubectl.
            print(f"{slug}: ping failed: {e!r}", file=sys.stderr)
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
