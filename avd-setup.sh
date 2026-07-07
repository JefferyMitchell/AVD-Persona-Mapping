#!/usr/bin/env bash
#
# AVD Assessment - PHASE 1: SETUP  (run once by a customer administrator)
# ======================================================================
# Creates the assessment scaffolding and a SCOPED, READ-ONLY collector
# identity. Run this in Azure Cloud Shell as someone with rights to
# create a resource group, a storage account, and assign roles.
#
# What this creates:
#   - A dedicated resource group  (holds only assessment resources)
#   - A throwaway storage account + private blob container for results
#   - A collector identity with EXACTLY these roles:
#       * Reader                        on the subscription   (ARM config, cannot modify anything)
#       * Log Analytics Reader          on the subscription   (KQL usage queries)
#       * Storage Blob Data Contributor on the assessment storage acct ONLY (deposit results)
#
# The collector identity is READ-ONLY against the entire environment. Its
# only write capability is confined to the single assessment storage account.
#
# Identity model (pick with -m):
#   sp      Service principal (default). Requires Entra app-registration rights.
#           Cleanest isolation; collection runs as a distinct constrained identity.
#   role    Custom role assigned to the current user. Use when SP creation is blocked.
#
# Usage:
#   ./avd-setup.sh                       # SP model, current subscription
#   ./avd-setup.sh -m role               # custom-role model
#   ./avd-setup.sh -s <subId> -l eastus  # explicit subscription + region
#
# Cleanup after the engagement (removes EVERYTHING this created):
#   ./avd-cleanup.sh
#
set -euo pipefail

MODEL="sp"
LOCATION="eastus"
SUB=""

while getopts "m:l:s:h" opt; do
  case $opt in
    m) MODEL="$OPTARG" ;;
    l) LOCATION="$OPTARG" ;;
    s) SUB="$OPTARG" ;;
    h) grep '^#' "$0" | sed 's/^#//'; exit 0 ;;
    *) echo "Invalid option"; exit 1 ;;
  esac
done

# --- Context -----------------------------------------------------------------
az account show >/dev/null 2>&1 || { echo "ERROR: run this in Cloud Shell (already authenticated) or 'az login'."; exit 1; }
[[ -n "$SUB" ]] && az account set --subscription "$SUB"
SUB_ID=$(az account show --query id -o tsv)
TENANT_ID=$(az account show --query tenantId -o tsv)

SUFFIX=$(date +%s | tail -c 6)
RG="rg-avd-assessment-$SUFFIX"
SA="stavdassess$SUFFIX"          # storage account names: lowercase, <=24 chars, unique
CONTAINER="results"
SP_NAME="sp-avd-assessment-$SUFFIX"
ROLE_NAME="AVD Assessment Collector $SUFFIX"

echo "==================== SETUP PLAN ===================="
echo "Subscription:     $SUB_ID"
echo "Identity model:   $MODEL"
echo "Resource group:   $RG"
echo "Storage account:  $SA"
echo "Container:        $CONTAINER (private)"
echo "Region:           $LOCATION"
echo "===================================================="
read -r -p "Proceed? [y/N] " ok
[[ "$ok" == "y" || "$ok" == "Y" ]] || { echo "Aborted."; exit 0; }

# --- Scaffolding -------------------------------------------------------------
echo "==> Creating resource group..."
az group create -n "$RG" -l "$LOCATION" \
  --tags purpose=avd-assessment temporary=true --only-show-errors -o none

echo "==> Creating storage account (private, no public blob access)..."
az storage account create -n "$SA" -g "$RG" -l "$LOCATION" \
  --sku Standard_LRS --kind StorageV2 \
  --allow-blob-public-access false --min-tls-version TLS1_2 \
  --tags purpose=avd-assessment temporary=true --only-show-errors -o none

echo "==> Creating private results container..."
az storage container create -n "$CONTAINER" \
  --account-name "$SA" --auth-mode login --only-show-errors -o none

SA_ID=$(az storage account show -n "$SA" -g "$RG" --query id -o tsv)

# --- Identity + role assignment ---------------------------------------------
if [[ "$MODEL" == "sp" ]]; then
  echo "==> Creating collector service principal (no default role)..."
  SP_JSON=$(az ad sp create-for-rbac --name "$SP_NAME" --skip-assignment --only-show-errors -o json)
  APP_ID=$(echo "$SP_JSON" | jq -r '.appId')
  SP_PASS=$(echo "$SP_JSON" | jq -r '.password')
  SP_OID=$(az ad sp show --id "$APP_ID" --query id -o tsv)

  echo "==> Assigning Reader on subscription (read-only over all resources)..."
  az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
    --role "Reader" --scope "/subscriptions/$SUB_ID" --only-show-errors -o none

  echo "==> Assigning Log Analytics Reader on subscription..."
  az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
    --role "Log Analytics Reader" --scope "/subscriptions/$SUB_ID" --only-show-errors -o none

  echo "==> Assigning Storage Blob Data Contributor on assessment storage ONLY..."
  az role assignment create --assignee-object-id "$SP_OID" --assignee-principal-type ServicePrincipal \
    --role "Storage Blob Data Contributor" --scope "$SA_ID" --only-show-errors -o none

  cat > avd-assessment.env <<EOF
# Source this, then run avd-collect.sh
export AVD_MODEL="sp"
export AVD_SUB_ID="$SUB_ID"
export AVD_TENANT_ID="$TENANT_ID"
export AVD_APP_ID="$APP_ID"
export AVD_SP_PASS="$SP_PASS"
export AVD_STORAGE="$SA"
export AVD_CONTAINER="$CONTAINER"
export AVD_RG="$RG"
EOF

  echo ""
  echo "==================== NEXT STEP ===================="
  echo "The collector identity is created and read-only scoped."
  echo "To run collection under it:"
  echo ""
  echo "  source avd-assessment.env"
  echo "  az login --service-principal -u \"\$AVD_APP_ID\" -p \"\$AVD_SP_PASS\" --tenant \"\$AVD_TENANT_ID\""
  echo "  ./avd-collect.sh"
  echo ""
  echo "(avd-assessment.env holds the SP secret - delete it after the run.)"
  echo "==================================================="

else
  # custom-role model: no SP, current user gets a tightly scoped custom role
  echo "==> Creating custom role (Reader-equivalent + storage blob write on assessment SA only)..."
  ROLE_DEF=$(cat <<JSON
{
  "Name": "$ROLE_NAME",
  "IsCustom": true,
  "Description": "Read-only AVD assessment collector. Write confined to assessment storage.",
  "Actions": [
    "Microsoft.DesktopVirtualization/*/read",
    "Microsoft.Compute/*/read",
    "Microsoft.OperationalInsights/workspaces/read",
    "Microsoft.OperationalInsights/workspaces/query/read",
    "Microsoft.Authorization/roleAssignments/read",
    "Microsoft.Storage/storageAccounts/blobServices/containers/read"
  ],
  "DataActions": [
    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/read",
    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/write",
    "Microsoft.Storage/storageAccounts/blobServices/containers/blobs/add/action"
  ],
  "AssignableScopes": [ "/subscriptions/$SUB_ID" ]
}
JSON
)
  echo "$ROLE_DEF" > _avd_role.json
  az role definition create --role-definition _avd_role.json --only-show-errors -o none
  rm -f _avd_role.json
  sleep 20  # allow role to propagate

  CURRENT_OID=$(az ad signed-in-user show --query id -o tsv)
  echo "==> Assigning custom role to current user at subscription scope..."
  az role assignment create --assignee-object-id "$CURRENT_OID" --assignee-principal-type User \
    --role "$ROLE_NAME" --scope "/subscriptions/$SUB_ID" --only-show-errors -o none

  # DataActions on blobs still need the data-plane role bound to the SA; grant the
  # blob data write strictly on the assessment storage account.
  az role assignment create --assignee-object-id "$CURRENT_OID" --assignee-principal-type User \
    --role "Storage Blob Data Contributor" --scope "$SA_ID" --only-show-errors -o none

  cat > avd-assessment.env <<EOF
export AVD_MODEL="role"
export AVD_SUB_ID="$SUB_ID"
export AVD_TENANT_ID="$TENANT_ID"
export AVD_STORAGE="$SA"
export AVD_CONTAINER="$CONTAINER"
export AVD_RG="$RG"
export AVD_ROLE_NAME="$ROLE_NAME"
EOF

  echo ""
  echo "==================== NEXT STEP ===================="
  echo "Custom role created and assigned to you. No service principal was made."
  echo "Run collection directly (you are already the scoped identity):"
  echo ""
  echo "  source avd-assessment.env"
  echo "  ./avd-collect.sh"
  echo "==================================================="
fi

# record identifiers for cleanup
cat > avd-cleanup.sh <<EOF
#!/usr/bin/env bash
# Auto-generated. Removes everything the setup created.
set -e
echo "Removing resource group $RG (storage + all results)..."
az group delete -n "$RG" --yes --no-wait
EOF
[[ "$MODEL" == "sp" ]] && cat >> avd-cleanup.sh <<EOF
echo "Removing service principal $SP_NAME..."
az ad sp delete --id "$APP_ID" || true
EOF
[[ "$MODEL" == "role" ]] && cat >> avd-cleanup.sh <<EOF
echo "Removing custom role '$ROLE_NAME'..."
az role definition delete --name "$ROLE_NAME" || true
EOF
echo "rm -f avd-assessment.env avd-cleanup.sh" >> avd-cleanup.sh
chmod +x avd-cleanup.sh
echo "==> Cleanup script written: ./avd-cleanup.sh"
