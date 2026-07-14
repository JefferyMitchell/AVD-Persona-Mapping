---
layout: default
title: Phase 1 - Setup
nav_order: 3
---

# Phase 1 — Setup
{: .no_toc }

Run once by a customer administrator. Creates the assessment scaffolding and the scoped, read-only collector identity.

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## What it creates

- A dedicated **resource group** (tagged `temporary=true`) that holds only assessment resources
- A private **storage account** (public blob access disabled, TLS 1.2 minimum) and a `results` container
- A **collector identity** with EXACTLY these three role assignments:

| Role | Scope | Why |
|---|---|---|
| **Reader** | Subscription | Read all ARM configuration; cannot modify anything |
| **Log Analytics Reader** | Subscription | Run the KQL usage queries |
| **Storage Blob Data Contributor** | The assessment storage account **only** | Deposit results — the identity's single write capability |

It also writes two files into your Cloud Shell working directory:

- `avd-assessment.env` — context for Phase 2 (source it before collecting)
- `avd-cleanup.sh` — one-command teardown of everything created

## Run it

```bash
./avd-setup.sh                       # SP model, current subscription
```

Other options:

```bash
./avd-setup.sh -m role               # custom-role model (when SP creation is locked down)
./avd-setup.sh -s <subId> -l eastus  # explicit subscription + region
```

The script prints a **setup plan** and asks you to confirm before creating anything:

```
==================== SETUP PLAN ====================
Subscription:     00000000-0000-0000-0000-000000000000
Identity model:   sp
Resource group:   rg-avd-assessment-12345
Storage account:  stavdassess12345
Container:        results (private)
Region:           eastus
====================================================
Proceed? [y/N]
```

## Identity models

Pick with `-m`:

| `-m` | Model | Use when |
|---|---|---|
| `sp` (default) | **Service principal** with no default role, then the three roles above assigned explicitly | You have Entra app-registration rights. Cleanest isolation — collection runs as a distinct, constrained identity. |
| `role` | A **custom role** assigned to your current user | Service principal creation is blocked. Includes a 20-second wait for RBAC propagation. |

> **Which should a security reviewer prefer?** The `sp` model, because collection runs as a separate identity that exists only for this engagement and is deleted at cleanup. The `role` model reuses your own user identity but constrains it with a purpose-built custom role.

## After it finishes

**SP model** — the collector is a service principal, so Phase 2 logs in as it:

```bash
source avd-assessment.env
az login --service-principal -u "$AVD_APP_ID" -p "$AVD_SP_PASS" --tenant "$AVD_TENANT_ID"
```

**Role model** — you *are* the scoped identity, so no separate login is needed:

```bash
source avd-assessment.env
```

> `avd-assessment.env` holds the service principal secret in the `sp` model. Delete it after the run (cleanup does this for you).

---

Next: [Phase 2 — Collect](phase-2-collect.md)
