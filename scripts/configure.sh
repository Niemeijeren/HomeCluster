#!/usr/bin/env bash
# Writes the values from config.env into the manifests.
# Re-runnable: it replaces whatever was applied last (scripts/.applied.env).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f config.env ]] || { echo "Create config.env first: cp config.env.example config.env" >&2; exit 1; }

load() { (set -a; source "$1"; for v in "${VARS[@]}"; do printf '%s\0' "${!v}"; done); }
# ACME_EMAIL before DOMAIN: the email may contain the domain (you@mail.<domain>).
VARS=(ACME_EMAIL DOMAIN REPO_URL MEDIA_PATH MEDIA_UID S3_BUCKET S3_ENDPOINT NODE_IP LAN_CLIENT_IP)
mapfile -d '' OLD < <(load scripts/.applied.env)
mapfile -d '' NEW < <(load config.env)

mapfile -d '' FILES < <(find argocd bootstrap infrastructure apps -name '*.yaml' -print0)
# Not YAML, but they hold values too: CI's allowed repo and LAN exception, and
# the hostnames the ingress health check probes.
FILES+=(scripts/check-policy.py apps/monitoring/check.py)

for i in "${!VARS[@]}"; do
  var=${VARS[$i]} old=${OLD[$i]} new=${NEW[$i]}
  [[ -n $new ]] || { echo "$var is empty in config.env" >&2; exit 1; }
  [[ $old == "$new" ]] && continue
  # Each value is only replaced in the context it appears in, so that e.g. a
  # domain inside the ACME email or a UID elsewhere is never touched.
  case $var in
    # <app>.example.com and the bare apex, but not the domain inside an email.
    DOMAIN)     re='(?<![\w@-])\Q$ENV{O}\E\b' ;;
    MEDIA_UID)  re='(?<=runAs(?:User|Group): )\Q$ENV{O}\E$' ;;
    *_IP)       re='(?<![\d.])\Q$ENV{O}\E(?![\d])' ;;   # not inside a longer IP
    *)          re='\Q$ENV{O}\E' ;;
  esac
  # A value that's gone (e.g. another variable's rewrite already changed it)
  # would silently stay unreplaced and leave .applied.env out of step.
  grep -qrF -- "$old" "${FILES[@]}" || echo "WARNING: $var: '$old' not found in any file, check by hand" >&2
  O=$old N=$new perl -pi -e "s/$re/\$ENV{N}/g" "${FILES[@]}"
  echo "$var: $old -> $new"
done

{ sed -n '1,2p' scripts/.applied.env; grep -v '^#' config.env | grep -v '^$'; } > scripts/.applied.env.tmp
mv scripts/.applied.env.tmp scripts/.applied.env
echo "Done. Review with: git diff"
