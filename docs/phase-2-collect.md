---
layout: default
title: Phase 2 - Collect
nav_order: 4
---

# Phase 2 — Collect
{: .no_toc }

Runs under the scoped read-only identity created in Phase 1. Pulls configuration and usage, hashes usernames, and uploads the results.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## Run it

First load the context written by Phase 1, then (SP model only) log in as the collector identity:

```bash
source avd-assessment.env

# SP model only — skip this if you used -m role:
az login --service-principal -u "$AVD_APP_ID" -p "$AVD_SP_PASS" --tenant "$AVD_TENANT_ID"
```

Then collect:

```bash
./avd-collect.sh            # default: 30-day lookback, usernames hashed
```

Options:

```bash
./avd-collect.sh -d 90      # 90-day lookback
./avd-collect.sh -r         # keep RAW usernames (not hashed)
```

> **This session is read-only against your environment.** The only write it performs is uploading the results to the single assessment storage account.

## What gets collected

**Configuration (ARM)** — always available:

| Output | Contents | Why it matters |
|---|---|---|
| `hostpools.csv` / `.json` | Host pool type, load balancer, max session limit, start-VM-on-connect, validation environment | Inventory, sizing, config baseline |
| `sessionhosts.csv` | Session host status, live sessions, allowNewSession | Host counts and current state |
| `appgroups.csv` | Desktop vs RemoteApp, assigned principal count | Entitlement counts |

**Usage (Log Analytics / KQL)** — requires diagnostics, see [Prerequisites](prerequisites.md#usage-data-requires-diagnostics-important):

| Output | Contents | Why it matters |
|---|---|---|
| `concurrency_<ws>.json` | Peak concurrent sessions per hour | **Sizing** the target footprint |
| `density_<ws>.json` | Sessions per host vs. configured max | **Right-sizing** — over/under-provisioning |
| `users_<ws>.csv` | Named users, session counts, last seen | **Shelfware detection** — named vs actually active |

**Run metadata:**

| Output | Contents |
|---|---|
| `manifest.json` | Lookback window, hashed-usernames flag, host pool count, timestamp |

The script discovers Log Analytics workspaces automatically and only queries those that actually contain AVD (`WVDConnections`) data.

## How usernames are protected

By default, every username is **SHA-256 hashed with a per-run random salt before it is ever written to disk** — the raw value never lands in a file.

- The salt is random per run, so hashes cannot be reversed with precomputed tables.
- The hash is **consistent within a single dataset**, so the same user maps to the same reference throughout — which is all that's needed to count named-vs-active users and spot shelfware.

Pass `-r` only if you have a specific reason to share real usernames, and only with your security team's agreement.

## What you'll see

```
==> Lookback: 30d | usernames: hashed
==> Pulling host pool configuration...
    3 host pool(s)
    - hp-knowledge-workers (Pooled, 12 hosts, 2 app groups)
    - hp-task-workers (Pooled, 8 hosts, 1 app groups)
    - hp-developers (Personal, 15 hosts, 1 app groups)
==> Discovering workspaces with AVD data...
    workspace law-avd-prod has AVD data
==> Uploading results to stavdassess12345/results/avd-data-20260714-101500/ ...

==================== DONE ====================
Host pools:  3
Uploaded to: stavdassess12345 / results / avd-data-20260714-101500/
```

The script then prints the exact command to generate the share link.

---

Next: [Share the Results](sharing.md)
