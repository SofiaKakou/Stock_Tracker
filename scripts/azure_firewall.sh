#!/usr/bin/env bash
# Opens (or closes) the Azure SQL firewall for this GitHub machine's own address, so the database never
# has to accept connections from everywhere. Uses a small Azure "service principal" (AZURE_CLIENT_ID,
# AZURE_CLIENT_SECRET, AZURE_TENANT_ID) that may only change that server's firewall rules.
# Without those secrets it does nothing (then the server must allow GitHub's addresses itself).
set -euo pipefail
action="${1:?open or close}"
if [ -z "${AZURE_CLIENT_ID:-}" ] || [ -z "${AZURE_SQL_SERVER:-}" ]; then
  echo "[journal] no Azure service principal; firewall left as it is"
  exit 0
fi
server="${AZURE_SQL_SERVER%%.*}"
rule="github-${GITHUB_RUN_ID:-local}"
az login --service-principal -u "$AZURE_CLIENT_ID" -p "$AZURE_CLIENT_SECRET" --tenant "$AZURE_TENANT_ID" --output none
if [ "$action" = open ]; then
  ip="$(curl -fsS https://api.ipify.org)"
  az sql server firewall-rule create -g "$AZURE_RESOURCE_GROUP" -s "$server" -n "$rule" \
    --start-ip-address "$ip" --end-ip-address "$ip" --output none
  echo "[journal] firewall opened for this run's address"
else
  az sql server firewall-rule delete -g "$AZURE_RESOURCE_GROUP" -s "$server" -n "$rule" --output none || true
  echo "[journal] firewall rule removed"
fi
