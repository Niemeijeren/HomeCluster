#!/usr/bin/env bash
# Writes the values from config.env into the manifests.
# Re-runnable: it replaces whatever was applied last (scripts/.applied.env).
set -euo pipefail
cd "$(dirname "$0")/.."

[[ -f config.env ]] || { echo "Create config.env first: cp config.env.example config.env" >&2; exit 1; }

load() { (set -a; source "$1"; for v in "${VARS[@]}"; do printf '%s\0' "${!v}"; done); }
VARS=(DOMAIN ACME_EMAIL REPO_URL MEDIA_PATH MEDIA_UID S3_BUCKET S3_ENDPOINT)
mapfile -d '' OLD < <(load scripts/.applied.env)
mapfile -d '' NEW < <(load config.env)

mapfile -d '' FILES < <(find argocd bootstrap infrastructure apps -name '*.yaml' -print0)

for i in "${!VARS[@]}"; do
  var=${VARS[$i]} old=${OLD[$i]} new=${NEW[$i]}
  [[ -n $new ]] || { echo "$var is empty in config.env" >&2; exit 1; }
  [[ $old == "$new" ]] && continue
  # Each value is only replaced in the context it appears in, so that e.g. a
  # domain inside the ACME email or a UID elsewhere is never touched.
  case $var in
    DOMAIN)     re='(?<=\.)\Q$ENV{O}\E\b' ;;          # <app>.example.com
    MEDIA_UID)  re='(?<=runAs(?:User|Group): )\Q$ENV{O}\E$' ;;
    *)          re='\Q$ENV{O}\E' ;;
  esac
  O=$old N=$new perl -pi -e "s/$re/\$ENV{N}/g" "${FILES[@]}"
  echo "$var: $old -> $new"
done

{ sed -n '1,2p' scripts/.applied.env; grep -v '^#' config.env | grep -v '^$'; } > scripts/.applied.env.tmp
mv scripts/.applied.env.tmp scripts/.applied.env
echo "Done. Review with: git diff"
