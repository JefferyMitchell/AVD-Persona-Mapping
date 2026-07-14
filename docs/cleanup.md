---
layout: default
title: Cleanup
nav_order: 6
---

# Cleanup
{: .no_toc }

One command removes everything the toolkit created. Run it once the architect has downloaded the results.

---

## Run it

`avd-cleanup.sh` was generated for you by Phase 1 with your specific resource names baked in:

```bash
./avd-cleanup.sh
```

## What it removes

| Removed | Effect |
|---|---|
| The assessment **resource group** | Deletes the storage account and every collected result inside it |
| The **service principal** (`sp` model) | The collector identity ceases to exist |
| The **custom role** (`role` model) | The scoped role definition is deleted |
| `avd-assessment.env` and `avd-cleanup.sh` | Removes the local context file holding the SP secret |

> **This also invalidates any SAS link you shared**, because the storage account is gone. Confirm the architect has downloaded the results before running cleanup.

## Verify nothing is left behind

```bash
# Resource group should be gone (or deleting)
az group list --query "[?tags.purpose=='avd-assessment'].name" -o tsv

# No leftover assessment service principals
az ad sp list --display-name "sp-avd-assessment" --query "[].displayName" -o tsv

# No leftover custom roles
az role definition list --custom-role-only true \
  --query "[?starts_with(roleName,'AVD Assessment Collector')].roleName" -o tsv
```

All three should return nothing. The resource group delete runs with `--no-wait`, so give it a minute if it still appears.

## If you lost the cleanup script

Everything the toolkit creates is tagged `purpose=avd-assessment temporary=true`, so you can find and remove it by tag:

```bash
# Find the resource group
az group list --query "[?tags.purpose=='avd-assessment'].name" -o tsv

# Delete it
az group delete -n <resource-group-name> --yes
```

Then delete the service principal or custom role by name as shown above.

---

Next: [Troubleshooting](troubleshooting.md)
