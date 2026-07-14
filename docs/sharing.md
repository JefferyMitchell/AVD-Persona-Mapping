---
layout: default
title: Share the Results
nav_order: 5
---

# Share the Results
{: .no_toc }

The results sit in **your** storage account. Nothing is shared until you explicitly generate a link.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## Review before you share

The output is plain JSON and CSV — read it first. Everything is in the run folder Phase 2 printed (`avd-data-<timestamp>/`).

```bash
ls avd-data-*/
cat avd-data-*/manifest.json
head avd-data-*/hostpools.csv
```

Check `manifest.json` confirms `"usernamesHashed": true` if that's what you expect.

## Generate a read-only SAS link

Phase 2 prints this command with your values already filled in. It creates a **read-only** (`rl` = read + list), **7-day** shared access signature scoped to the results container only:

```bash
az storage container generate-sas \
  --account-name "$AVD_STORAGE" \
  --name "$AVD_CONTAINER" \
  --permissions rl \
  --expiry $(date -u -d '+7 days' '+%Y-%m-%dT%H:%MZ') \
  --auth-mode login --as-user -o tsv
```

That outputs a SAS token. The full URL to share is:

```
https://<storage-account>.blob.core.windows.net/results?<sas-token>
```

| Property | Value |
|---|---|
| Permissions | Read + List only — the holder cannot write, modify, or delete |
| Scope | The `results` container only — nothing else in your subscription |
| Lifetime | 7 days, then it stops working |
| Revocation | Delete the storage account (or run [cleanup](cleanup.md)) to invalidate immediately |

> Adjust `--permissions` or `--expiry` if your security policy requires something tighter. `rl` for 7 days is the recommended default.

## Send it to the architect

Share the URL through whatever channel your organisation uses for time-limited credentials. The architect downloads the container contents and builds the persona mapping and sizing analysis from it.

## What the architect does with it

| Data | Analysis |
|---|---|
| Concurrency peaks | Sizes the target Citrix footprint against real peak demand, not licence counts |
| Density vs. configured max | Identifies over- and under-provisioned host pools |
| Named vs. active users | Quantifies shelfware — entitlements nobody uses |
| Host pool + app group config | Maps existing workloads onto Citrix DaaS / Flex personas |

---

Next: [Cleanup](cleanup.md)
