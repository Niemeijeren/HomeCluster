#!/usr/bin/env python3
"""Render every Helm chart Application in argocd/apps/ the way Argo CD does.

Our values (bootstrap/argocd-values.yaml, the inline valuesObject blocks) are
our code, so CI should catch a typo before Argo CD does. The output is meant
for kubeconform only: the charts themselves are upstream code, outside
scripts/check-policy.py.

    python3 scripts/render-charts.py > charts.yaml
"""
import glob
import os
import subprocess
import sys
import tempfile

import yaml

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
# Keep in step with the cluster (`kubectl version`) when k3s is upgraded.
KUBE_VERSION = "1.36.5"


def render(app):
    spec = app["spec"]
    sources = spec.get("sources") or [spec["source"]]
    for src in sources:
        if "chart" not in src:
            continue
        helm = src.get("helm", {})
        cmd = [
            "helm", "template", "--repo", src["repoURL"], "--version", src["targetRevision"],
            "--namespace", spec["destination"].get("namespace", "default"),
            # Argo passes the cluster's version; charts may branch on it.
            "--kube-version", KUBE_VERSION,
        ]
        for f in helm.get("valueFiles", []):
            # `$values/<path>` points at the homelab repo (the `ref: values` source).
            path = os.path.realpath(os.path.join(REPO_ROOT, f.removeprefix("$values/")))
            if not path.startswith(REPO_ROOT + os.sep):
                sys.exit(f"{app['metadata']['name']}: valueFile {f!r} is outside the repo")
            cmd += ["-f", path]
        with tempfile.NamedTemporaryFile("w", suffix=".yaml") as values:
            if "valuesObject" in helm:
                yaml.safe_dump(helm["valuesObject"], values)
                values.flush()
                cmd += ["-f", values.name]
            # `--` ends the flags, so a chart or release name like
            # "--post-renderer=..." can't be parsed as one.
            cmd += ["--", helm.get("releaseName", app["metadata"]["name"]), src["chart"]]
            print(f"render: {app['metadata']['name']} ({src['chart']} {src['targetRevision']})", file=sys.stderr)
            out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
        yield out


def main():
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "argocd/apps/*.yaml"))):
        with open(path) as f:
            app = yaml.safe_load(f)
        # argocd/apps also holds AppProjects, which have no sources.
        if app.get("kind") != "Application":
            continue
        for out in render(app):
            sys.stdout.write(out)
            sys.stdout.write("\n---\n")


if __name__ == "__main__":
    main()
