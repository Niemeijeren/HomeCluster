#!/usr/bin/env bash
# Seal one secret value without it touching the chat, shell history or disk:
# it's read hidden from the terminal and piped straight into kubeseal.
# Usage: scripts/seal-from-prompt.sh <namespace> <secret-name> <key> <out.sealed.yaml>
set -euo pipefail
[ $# -eq 4 ] || { echo "usage: $0 <namespace> <secret-name> <key> <out.sealed.yaml>" >&2; exit 2; }
ns=$1 name=$2 key=$3 out=$4

IFS= read -rsp "value for $ns/$name ($key), hidden: " value
echo >&2
[ -n "$value" ] || { echo "empty value, nothing written" >&2; exit 1; }

# All or nothing: an empty or half-written file, if committed, would make Argo
# CD prune the SealedSecret and delete the real Secret.
tmp=$(mktemp "$out.XXXXXX")
trap 'rm -f "$tmp"' EXIT
printf '%s' "$value" \
  | kubectl -n "$ns" create secret generic "$name" --from-file="$key"=/dev/stdin \
      --dry-run=client -o yaml \
  | kubeseal --format yaml > "$tmp"
[ -s "$tmp" ] || { echo "kubeseal produced nothing, $out left unchanged" >&2; exit 1; }
mv "$tmp" "$out"
unset value
echo "wrote $out (sealed for $ns/$name)" >&2
