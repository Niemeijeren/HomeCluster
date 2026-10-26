#!/usr/bin/env bash
# Creates the five SealedSecrets this repo needs, encrypted with the public key
# of the sealed-secrets controller in your current kubectl context.
# Plaintext never touches the disk: it is piped straight into kubeseal.
# Needs: kubectl, kubeseal, openssl, and a running sealed-secrets controller.
set -euo pipefail
cd "$(dirname "$0")/.."

for bin in kubectl kubeseal openssl; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done

ask() { local v; read -rsp "$1: " v; echo >&2; [[ -n $v ]] || { echo "empty" >&2; exit 1; }; printf '%s' "$v"; }

seal() { # seal <namespace> <name> <output file> <kubectl create secret args...>
  local ns=$1 name=$2 out=$3; shift 3
  kubectl -n "$ns" create secret generic "$name" "$@" --dry-run=client -o yaml \
    | kubeseal --format yaml > "$out"
  echo "wrote $out" >&2
}

CF_TOKEN=$(ask "Cloudflare API token (Zone:DNS:Edit on your domain)")
S3_KEY_ID=$(ask "S3 access key ID (scoped to the backup bucket)")
S3_SECRET=$(ask "S3 secret access key")
HC_PING_KEY=$(ask "healthchecks.io project ping key (project Settings, API Access)")
# Each Kopia repository gets its own random encryption password.
VELERO_REPO_PW=$(openssl rand -base64 32)
MEDIA_REPO_PW=$(openssl rand -base64 32)

seal cert-manager cloudflare-api-token-secret \
  infrastructure/cert-manager/cloudflare-api-token.sealed.yaml \
  --from-literal=api-token="$CF_TOKEN"

seal velero velero-r2-credentials infrastructure/velero/r2-credentials.sealed.yaml \
  --from-file=cloud=<(printf '[default]\naws_access_key_id=%s\naws_secret_access_key=%s\n' \
    "$S3_KEY_ID" "$S3_SECRET")

seal velero velero-repo-credentials infrastructure/velero/repo-credentials.sealed.yaml \
  --from-literal=repository-password="$VELERO_REPO_PW"

seal media-backup media-backup-credentials apps/media-backup/credentials.sealed.yaml \
  --from-literal=AWS_ACCESS_KEY_ID="$S3_KEY_ID" \
  --from-literal=AWS_SECRET_ACCESS_KEY="$S3_SECRET" \
  --from-literal=KOPIA_PASSWORD="$MEDIA_REPO_PW"

seal monitoring healthchecks apps/monitoring/healthchecks.sealed.yaml \
  --from-literal=ping-key="$HC_PING_KEY"

cat >&2 <<MSG

Store these two passwords in a password manager NOW. Without them (or the
sealing key) your offsite backups cannot be decrypted. They are not saved anywhere else.

  Velero Kopia repository password: $VELERO_REPO_PW
  Media  Kopia repository password: $MEDIA_REPO_PW

Then back up the controller's sealing key (see README, "Secrets").
MSG
