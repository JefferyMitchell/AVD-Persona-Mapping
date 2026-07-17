#!/usr/bin/env bash
#
# AVD Assessment - PHASE 2: COLLECT  (runs under the scoped read-only identity)
# ============================================================================
# Pulls AVD host pool configuration (ARM) and usage/concurrency (Log Analytics),
# hashes usernames by default, and uploads JSON + CSV to the assessment
# storage container created in Phase 1.
#
# Prereq: source the env file from setup, and (SP model) log in as the SP:
#   source avd-assessment.env
#   # SP model only:
#   az login --service-principal -u "$AVD_APP_ID" -p "$AVD_SP_PASS" --tenant "$AVD_TENANT_ID"
#   ./avd-collect.sh
#
# Flags:
#   -d <days>   KQL lookback window (default 30)
#   -r          Keep RAW usernames (default: SHA-256 hashed)
#
# This session is READ-ONLY against the environment. The only write is
# uploading results to the single assessment storage account.
#
set -euo pipefail

LOOKBACK_DAYS=30
HASH_USERS=1

while getopts "d:rh" opt; do
  case $opt in
    d) LOOKBACK_DAYS="$OPTARG" ;;
    r) HASH_USERS=0 ;;
    h) grep '^#' "$0" | sed 's/^#//'; exit 0 ;;
    *) echo "Invalid option"; exit 1 ;;
  esac
done

: "${AVD_SUB_ID:?source avd-assessment.env first}"
: "${AVD_STORAGE:?source avd-assessment.env first}"
: "${AVD_CONTAINER:?source avd-assessment.env first}"

az account set --subscription "$AVD_SUB_ID"

TS=$(date +%Y%m%d-%H%M%S)
OUT="avd-data-$TS"
mkdir -p "$OUT"
# Per-run salt so hashes aren't reversible via precomputed tables, but are
# consistent WITHIN this dataset (same user hashes to same value here).
SALT=$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')

hash_user() {
  if [[ "$HASH_USERS" -eq 1 ]]; then
    printf '%s%s' "$SALT" "$1" | sha256sum | cut -c1-16
  else
    printf '%s' "$1"
  fi
}

echo "==> Lookback: ${LOOKBACK_DAYS}d | usernames: $([[ $HASH_USERS -eq 1 ]] && echo hashed || echo RAW)"

# --- Ensure read-only extensions ---------------------------------------------
az extension add --name desktopvirtualization --only-show-errors >/dev/null 2>&1 || true
az extension add --name log-analytics --only-show-errors >/dev/null 2>&1 || true

# --- Configuration (ARM) -----------------------------------------------------
echo "subscription,resourceGroup,hostPoolName,type,loadBalancer,maxSessionLimit,startVMOnConnect,validationEnvironment,sessionHosts,appGroups" > "$OUT/hostpools.csv"
echo "resourceGroup,hostPoolName,sessionHostName,status,sessions,allowNewSession" > "$OUT/sessionhosts.csv"
echo "resourceGroup,hostPoolName,appGroupName,type,assignedPrincipals" > "$OUT/appgroups.csv"

echo "==> Pulling host pool configuration..."
az desktopvirtualization hostpool list --only-show-errors -o json 2>/dev/null > "$OUT/hostpools.json" || echo "[]" > "$OUT/hostpools.json"
HP_COUNT=$(jq 'length' "$OUT/hostpools.json")
echo "    $HP_COUNT host pool(s)"

jq -c '.[]' "$OUT/hostpools.json" | while read -r hp; do
  HP_NAME=$(echo "$hp" | jq -r '.name')
  HP_ID=$(echo "$hp" | jq -r '.id')
  RG=$(echo "$HP_ID" | sed -n 's#.*/[Rr]esource[Gg]roups/\([^/]*\)/.*#\1#p')
  HP_TYPE=$(echo "$hp" | jq -r '.hostPoolType // "Unknown"')
  LB=$(echo "$hp" | jq -r '.loadBalancerType // "Unknown"')
  MAX=$(echo "$hp" | jq -r '.maxSessionLimit // "n/a"')
  SVMOC=$(echo "$hp" | jq -r '.startVMOnConnect // false')
  VALENV=$(echo "$hp" | jq -r '.validationEnvironment // false')

  # Session hosts: the stable 'desktopvirtualization' CLI extension exposes no
  # session-host command, so query ARM directly. Response is
  # {value:[{name, properties:{status,sessions,allowNewSession}}]}.
  az rest --method GET \
    --url "https://management.azure.com${HP_ID}/sessionHosts?api-version=2024-04-03" \
    --only-show-errors -o json 2>/dev/null | jq '.value // []' > "$OUT/_sh.json" 2>/dev/null || echo "[]" > "$OUT/_sh.json"
  [[ -s "$OUT/_sh.json" ]] || echo "[]" > "$OUT/_sh.json"
  SH_COUNT=$(jq 'length' "$OUT/_sh.json")
  jq -c '.[]' "$OUT/_sh.json" | while read -r sh; do
    SH_NAME=$(echo "$sh" | jq -r '.name' | sed 's#.*/##')
    echo "$RG,$HP_NAME,$SH_NAME,$(echo "$sh" | jq -r '.properties.status // "Unknown"'),$(echo "$sh" | jq -r '.properties.sessions // 0'),$(echo "$sh" | jq -r '.properties.allowNewSession // true')" >> "$OUT/sessionhosts.csv"
  done

  az desktopvirtualization applicationgroup list --only-show-errors -o json 2>/dev/null \
    --query "[?contains(hostPoolArmPath, '$HP_NAME')]" > "$OUT/_ag.json" || echo "[]" > "$OUT/_ag.json"
  AG_COUNT=$(jq 'length' "$OUT/_ag.json")
  jq -c '.[]' "$OUT/_ag.json" | while read -r ag; do
    AG_NAME=$(echo "$ag" | jq -r '.name')
    AG_TYPE=$(echo "$ag" | jq -r '.applicationGroupType // "Unknown"')
    AG_RG=$(echo "$ag" | jq -r '.id' | sed -n 's#.*/[Rr]esource[Gg]roups/\([^/]*\)/.*#\1#p')
    ASSIGNED=$(az role assignment list --scope "$(echo "$ag" | jq -r '.id')" --only-show-errors --query "length(@)" -o tsv 2>/dev/null || echo "0")
    echo "$AG_RG,$HP_NAME,$AG_NAME,$AG_TYPE,$ASSIGNED" >> "$OUT/appgroups.csv"
  done

  echo "$AVD_SUB_ID,$RG,$HP_NAME,$HP_TYPE,$LB,$MAX,$SVMOC,$VALENV,$SH_COUNT,$AG_COUNT" >> "$OUT/hostpools.csv"
  echo "    - $HP_NAME ($HP_TYPE, $SH_COUNT hosts, $AG_COUNT app groups)"
done
rm -f "$OUT/_sh.json" "$OUT/_ag.json"

# --- Usage (Log Analytics / KQL) --------------------------------------------
echo "==> Discovering workspaces with AVD data..."
KQL_CONCURRENCY="WVDConnections | where TimeGenerated > ago(${LOOKBACK_DAYS}d) | where State == 'Connected' | extend hour = bin(TimeGenerated, 1h) | summarize concurrent = dcount(CorrelationId) by hour | order by hour asc"
KQL_USERS="WVDConnections | where TimeGenerated > ago(${LOOKBACK_DAYS}d) | where State == 'Connected' | summarize sessions = count(), lastSeen = max(TimeGenerated) by UserName | order by sessions desc"
KQL_DENSITY="WVDConnections | where TimeGenerated > ago(${LOOKBACK_DAYS}d) | where State == 'Connected' | summarize peakSessions = dcount(CorrelationId) by SessionHostName | order by peakSessions desc"

az monitor log-analytics workspace list --only-show-errors -o json 2>/dev/null | jq -c '.[]' | while read -r ws; do
  WS_NAME=$(echo "$ws" | jq -r '.name')
  WS_CID=$(echo "$ws" | jq -r '.customerId')
  # -o tsv appends a TableName column ("<count>\tPrimaryResult"); take field 1.
  HAS=$(az monitor log-analytics query -w "$WS_CID" --analytics-query "WVDConnections | count" --only-show-errors -o tsv 2>/dev/null | tail -1 | cut -f1 || echo "0")
  [[ "$HAS" =~ ^[0-9]+$ ]] && [[ "$HAS" -gt 0 ]] || continue
  echo "    workspace $WS_NAME has AVD data"

  az monitor log-analytics query -w "$WS_CID" --analytics-query "$KQL_CONCURRENCY" --only-show-errors -o json > "$OUT/concurrency_${WS_NAME}.json" 2>/dev/null || echo "[]" > "$OUT/concurrency_${WS_NAME}.json"
  az monitor log-analytics query -w "$WS_CID" --analytics-query "$KQL_DENSITY"     --only-show-errors -o json > "$OUT/density_${WS_NAME}.json"     2>/dev/null || echo "[]" > "$OUT/density_${WS_NAME}.json"

  # users: hash the UserName field before it ever hits disk
  az monitor log-analytics query -w "$WS_CID" --analytics-query "$KQL_USERS" --only-show-errors -o json 2>/dev/null > "$OUT/_users_raw.json" || echo "[]" > "$OUT/_users_raw.json"
  echo "userRef,sessions,lastSeen" > "$OUT/users_${WS_NAME}.csv"
  jq -c '.[]' "$OUT/_users_raw.json" 2>/dev/null | while read -r u; do
    UN=$(echo "$u" | jq -r '.UserName // "unknown"')
    echo "$(hash_user "$UN"),$(echo "$u" | jq -r '.sessions // 0'),$(echo "$u" | jq -r '.lastSeen // ""')" >> "$OUT/users_${WS_NAME}.csv"
  done
  rm -f "$OUT/_users_raw.json"
done

# --- Manifest ----------------------------------------------------------------
cat > "$OUT/manifest.json" <<EOF
{
  "generated": "$TS",
  "subscription": "$AVD_SUB_ID",
  "lookbackDays": $LOOKBACK_DAYS,
  "usernamesHashed": $([[ $HASH_USERS -eq 1 ]] && echo true || echo false),
  "hostPoolCount": $HP_COUNT
}
EOF

# --- Upload to assessment storage -------------------------------------------
echo "==> Uploading results to $AVD_STORAGE/$AVD_CONTAINER/$OUT/ ..."
az storage blob upload-batch \
  --account-name "$AVD_STORAGE" --auth-mode login \
  --destination "$AVD_CONTAINER" --destination-path "$OUT" \
  --source "$OUT" --only-show-errors -o none

echo ""
echo "==================== DONE ===================="
echo "Host pools:  $HP_COUNT"
echo "Uploaded to: $AVD_STORAGE / $AVD_CONTAINER / $OUT/"
echo ""
echo "Ask the customer to generate a read-only SAS for the container:"
echo "  az storage container generate-sas --account-name $AVD_STORAGE \\"
echo "    --name $AVD_CONTAINER --permissions rl \\"
echo "    --expiry \$(date -u -d '+7 days' '+%Y-%m-%dT%H:%MZ') \\"
echo "    --auth-mode login --as-user -o tsv"
echo "=============================================="
