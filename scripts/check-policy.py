#!/usr/bin/env python3
"""Repo conventions that kubeconform can't check (see README "Conventions").

Scope: the manifests in this repo. Helm charts that Argo CD renders (argo-cd,
cert-manager, velero, sealed-secrets) are upstream code and not checked here;
scripts/render-charts.py renders them with our values for kubeconform only.

Reads rendered manifests (multi-document YAML) on stdin, prints every
violation and exits 1 if there are any. Locally, render the way CI does
(.github/workflows/validate.yaml, step "Render") and pipe that in:

    { for d in infrastructure/* apps/*; do kubectl kustomize "$d"; echo ---; done
      for f in argocd/apps/*.yaml bootstrap/root-app.yaml; do cat "$f"; echo ---; done
    } | python3 scripts/check-policy.py

The `echo ---` matters: plain `cat` of several files merges them into one
YAML document, and PyYAML silently keeps the last duplicate key.
"""
import sys

import yaml

# The one documented exception to "Services are ClusterIP" (README), with the
# exact source ranges it may have. Widening them is a reviewed code change.
ALLOWED_EXPOSED_SERVICES = {("jellyfin", "jellyfin-lan"): ["192.0.2.1/32"]}

# Sources an Application may deploy from. Pointing one at any other repo hands
# that repo cluster-admin (AppProject `default` allows everything).
ALLOWED_REPOS = {
    "https://github.com/your-github-user/HomeCluster.git",
    "https://argoproj.github.io/argo-helm",
    "https://bitnami.github.io/sealed-secrets",
    "https://charts.jetstack.io",
    "https://vmware-tanzu.github.io/helm-charts",
}

# Paths whose Application must use a restricted AppProject (argocd/apps/project-*.yaml).
RESTRICTED_PATHS = {"apps/website": "website"}

# hostPort and the host namespaces bypass Services, source ranges and ufw
# (the 2026-10-22 Jellyfin drift was a hostPort).
HOST_ACCESS_FIELDS = ("hostNetwork", "hostPID", "hostIPC")

WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}


def pod_spec(obj):
    kind, spec = obj["kind"], obj.get("spec", {})
    if kind == "Pod":
        return spec
    if kind == "CronJob":
        return spec.get("jobTemplate", {}).get("spec", {}).get("template", {}).get("spec", {})
    return spec.get("template", {}).get("spec", {})


def check(obj):
    kind = obj.get("kind")
    meta = obj.get("metadata", {})
    name = f"{kind} {meta.get('namespace', '-')}/{meta.get('name')}"

    if kind == "Secret":
        yield f"{name}: plain Secret in git, use a SealedSecret"

    if kind == "PersistentVolumeClaim":
        opts = meta.get("annotations", {}).get("argocd.argoproj.io/sync-options", "")
        if "Prune=false" not in opts or "Delete=false" not in opts:
            yield f"{name}: needs argocd.argoproj.io/sync-options: Prune=false,Delete=false"

    if kind in WORKLOAD_KINDS:
        spec = pod_spec(obj)
        for c in spec.get("containers", []) + spec.get("initContainers", []):
            image = c.get("image", "")
            last = image.rsplit("/", 1)[-1]
            if "@sha256:" not in image and (":" not in last or last.endswith(":latest")):
                yield f"{name}: container {c.get('name')} image {image!r} must be pinned (no :latest)"
            for port in c.get("ports", []):
                if "hostPort" in port:
                    yield f"{name}: container {c.get('name')} uses hostPort {port['hostPort']}"
        for field in HOST_ACCESS_FIELDS:
            if spec.get(field):
                yield f"{name}: {field}: true"

    if kind == "Service":
        spec = obj.get("spec", {})
        if spec.get("externalIPs"):
            yield f"{name}: externalIPs make kube-proxy answer on those IPs"
        if spec.get("type", "ClusterIP") != "ClusterIP":
            key = (meta.get("namespace"), meta.get("name"))
            if key not in ALLOWED_EXPOSED_SERVICES:
                yield f"{name}: type {spec.get('type')}; only ClusterIP (exceptions are listed in this script)"
            else:
                if spec.get("allocateLoadBalancerNodePorts") is not False:
                    yield f"{name}: exposed Service needs allocateLoadBalancerNodePorts: false"
                if spec.get("loadBalancerSourceRanges") != ALLOWED_EXPOSED_SERVICES[key]:
                    yield f"{name}: loadBalancerSourceRanges must be exactly {ALLOWED_EXPOSED_SERVICES[key]}"

    if kind == "Application":
        spec = obj.get("spec", {})
        if not spec.get("project"):
            yield f"{name}: project missing"
        retry = spec.get("syncPolicy", {}).get("retry")
        if not retry:
            yield f"{name}: syncPolicy.retry missing"
        elif retry.get("refresh") is not True:
            yield f"{name}: syncPolicy.retry.refresh must be true (else a fix waits out the retries)"
        for src in spec.get("sources") or [spec.get("source", {})]:
            if src.get("repoURL") not in ALLOWED_REPOS:
                yield f"{name}: repoURL {src.get('repoURL')!r} not in ALLOWED_REPOS"
            # Apps deployed by CI must stay inside their restricted AppProject.
            for path, project in RESTRICTED_PATHS.items():
                if src.get("path", "").rstrip("/") == path and spec.get("project") != project:
                    yield f"{name}: deploys {path} but project is {spec.get('project')!r}, must be {project!r}"


def is_default_deny(obj):
    spec = obj.get("spec", {})
    return (obj.get("kind") == "NetworkPolicy" and spec.get("podSelector") == {}
            and set(spec.get("policyTypes", [])) == {"Ingress", "Egress"}
            and not spec.get("ingress") and not spec.get("egress"))


def check_default_deny(objs):
    """Every namespace that runs our workloads starts from deny-all."""
    workload_ns = {o["metadata"].get("namespace") for o in objs if o.get("kind") in WORKLOAD_KINDS}
    denied_ns = {o["metadata"].get("namespace") for o in objs if is_default_deny(o)}
    for ns in sorted(n for n in workload_ns - denied_ns if n):
        yield f"namespace {ns}: workloads but no default-deny NetworkPolicy"


def main():
    objs = [obj for obj in yaml.safe_load_all(sys.stdin) if obj]
    errors = [e for obj in objs for e in check(obj)] + list(check_default_deny(objs))
    for e in errors:
        print(f"POLICY: {e}", file=sys.stderr)
    print(f"policy: {len(errors)} violation(s)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
