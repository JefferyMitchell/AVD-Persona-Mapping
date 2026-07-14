---
layout: default
title: Troubleshooting
nav_order: 7
---

# Troubleshooting
{: .no_toc }

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## Phase 1 — Setup

### "Insufficient privileges to complete the operation" when creating the service principal

Your account lacks Entra app-registration rights. Use the custom-role model instead — it creates no service principal:

```bash
./avd-setup.sh -m role
```

### "AuthorizationFailed" when assigning roles

Creating role assignments requires **Owner** or **User Access Administrator** at subscription scope. Contributor alone is not enough — it can create resources but cannot grant roles. Ask whoever holds that role to run Phase 1.

### Storage account name already taken

Storage account names are globally unique across all of Azure. The script derives a name from a timestamp suffix, so collisions are rare — simply re-run `./avd-setup.sh` to get a new suffix.

### The custom role isn't recognised immediately

RBAC changes take time to propagate. The `role` model already waits 20 seconds; if the role assignment still fails, wait a minute and re-run. Propagation can occasionally take several minutes.

---

## Phase 2 — Collect

### "source avd-assessment.env first"

The script needs the context Phase 1 wrote. Run it from the same directory:

```bash
source avd-assessment.env
./avd-collect.sh
```

If Cloud Shell timed out and the file is gone, re-run Phase 1 — or reconstruct the variables from the resource names in the portal.

### Zero host pools found

Check you are pointed at the right subscription:

```bash
az account show --query name -o tsv
```

The collector's **Reader** role is scoped to the subscription Phase 1 ran against. If your AVD estate lives in a different subscription, re-run Phase 1 there with `-s <subId>`. Multi-subscription collection in a single run is not yet supported — run the toolkit once per subscription.

### No usage data — only config files were produced

This is expected when the environment has no Azure Monitor diagnostics forwarding AVD logs to Log Analytics. The script queries only workspaces that actually contain `WVDConnections` data, and skips the usage sections if none do.

Verify whether any workspace has AVD data:

```bash
az monitor log-analytics query \
  -w <workspace-customer-id> \
  --analytics-query "WVDConnections | take 1 | count" -o table
```

If this returns 0, diagnostics are not configured or the data has aged out of retention. Config-only collection is still useful — and the lack of telemetry is itself a finding.

### Usage data looks thin or truncated

The default lookback is 30 days. If workspace retention is shorter than that, you only get what's retained. If retention is longer, widen the window:

```bash
./avd-collect.sh -d 90
```

### Upload fails with an authorization error

The **Storage Blob Data Contributor** assignment is scoped to the assessment storage account, and data-plane role assignments can take a few minutes to propagate. Wait a couple of minutes and re-run `./avd-collect.sh` — collection is safe to repeat, it just writes a new timestamped folder.

Confirm you are logged in as the collector identity (SP model):

```bash
az account show --query user.name -o tsv    # should be the app ID, not your user
```

---

## Sharing

### The SAS command fails with "auth-mode login" errors

`--as-user` requires you to be signed in as a user, not the service principal. Log back in as yourself before generating the SAS:

```bash
az login
source avd-assessment.env
```

### The architect says the link expired

SAS tokens are 7 days by default. Generate a fresh one — see [Share the Results](sharing.md#generate-a-read-only-sas-link). The underlying data is unchanged as long as you have not run [cleanup](cleanup.md).

---

## Still stuck?

Open an issue on [GitHub](https://github.com/JefferyMitchell/AVD-Persona-Mapping/issues) with the command you ran and the error output. Redact subscription IDs and tenant IDs.
