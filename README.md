# AVD Persona Mapping

A two-phase discovery toolkit that lets a customer safely share their
**Azure Virtual Desktop (AVD)** environment — configuration and real usage —
with a Citrix architect, so that migration and persona-mapping conversations
are grounded in the customer's actual numbers rather than assumptions.

The customer runs the scripts in their own Azure tenant. Nothing leaves their
control until *they* generate a time-limited, read-only link and share it.

📖 **Full guided documentation:** see the [`docs/`](docs/) folder (Prerequisites → Phase 1 → Phase 2 → Sharing → Cleanup → Troubleshooting).

> This repo is **private**. The docs are a Jekyll site but public GitHub Pages hosting is intentionally disabled — read the Markdown in `docs/` directly, or run `bundle exec jekyll serve` locally. Re-enabling Pages would republish the site publicly (this plan has no private-Pages option).

---

## Why this exists

To map an AVD estate to Citrix DaaS / Flex personas, an architect needs hard
data: host-pool sizing, concurrency, per-host density, entitlement counts, and
shelfware. This toolkit collects exactly that — and no more — under an identity
the customer's security team can audit line by line.

---

## Security model (the important part)

The customer's security team will scrutinize this, so the design is deliberately
split by privilege level:

- The **collector identity is read-only against the entire environment.** It
  genuinely cannot modify host pools, session hosts, VMs, or anything else.
- Its *only* write capability is confined to a **single throwaway storage
  account** created for the assessment.
- This is enforced by **role scoping**, not a vague "read-only account" claim —
  you can point to exactly three role assignments.

A single identity can't both *create* a storage account and be *read-only* —
creating and uploading are writes. So resource creation lives in the admin-run
setup phase; the collector identity is read-only-plus-one-storage-account.

**PII:** usernames are SHA-256 hashed with a per-run salt *before they ever hit
disk*. Pass `-r` to keep them raw only if the customer explicitly wants it.

---

## Prerequisites

- **Azure Cloud Shell** (Bash) — already has `az` and `jq`.
- Phase 1 must be run by someone who can create a resource group + storage
  account and assign roles at subscription scope.
- Usage data (concurrency / density / shelfware) requires the customer to have
  Azure Monitor diagnostics sending AVD logs to a Log Analytics workspace, with
  retention covering the lookback window. **No diagnostics → config only**
  (which is itself a talking point: they have no visibility into their own
  utilization). Config data is always available.

---

## How to run

### Phase 1 — Setup (customer admin, once)

Creates the resource group, private storage account, results container, and the
scoped read-only collector identity.

```bash
./avd-setup.sh                       # SP model, current subscription
./avd-setup.sh -m role               # custom-role model (when SP creation is locked down)
./avd-setup.sh -s <subId> -l eastus  # explicit subscription + region
```

Identity models:

| `-m` | Model | Use when |
|------|-------|----------|
| `sp` (default) | Service principal, no default role | You have Entra app-registration rights. Cleanest isolation. |
| `role` | Custom role assigned to the current user | SP creation is blocked. Includes a 20s RBAC-propagation wait. |

Setup writes `avd-assessment.env` (context for Phase 2) and `avd-cleanup.sh`
(one-command teardown).

### Phase 2 — Collect (runs under the scoped identity)

```bash
source avd-assessment.env
# SP model only:
az login --service-principal -u "$AVD_APP_ID" -p "$AVD_SP_PASS" --tenant "$AVD_TENANT_ID"

./avd-collect.sh            # default 30-day lookback, hashed usernames
./avd-collect.sh -d 90      # 90-day lookback
./avd-collect.sh -r         # keep raw usernames
```

Collection pulls config (ARM) + usage (KQL), hashes usernames, and uploads
JSON/CSV to the results container. It then prints the exact command for the
customer to generate a **read-only, 7-day SAS link** to share.

### Cleanup (after the engagement)

```bash
./avd-cleanup.sh            # deletes the RG (storage + results) and the SP/custom role
```

---

## What gets collected

| Source | Output | Why it matters |
|---|---|---|
| ARM | `hostpools.csv` / `.json` | Inventory, sizing, config baseline |
| ARM | `sessionhosts.csv` | Host counts, status, live sessions |
| ARM | `appgroups.csv` | Desktop vs RemoteApp, entitlement counts |
| KQL | `concurrency_<ws>.json` | Peak concurrent sessions/hour — **sizing** |
| KQL | `density_<ws>.json` | Users per host vs. configured max — **right-sizing** |
| KQL | `users_<ws>.csv` | Named vs active — **shelfware detection** |
| — | `manifest.json` | Run metadata (lookback, hashed flag, counts) |

---

## Data flow

```
[Customer Cloud Shell]
   avd-setup.sh   (admin)      ──> RG + storage + collector identity
   avd-collect.sh (collector)  ──> reads ARM + Log Analytics
                               ──> hashes usernames
                               ──> uploads JSON/CSV to results container
   customer generates read-only 7-day SAS ──> shares link
[Architect] downloads container ──> avd-analyze.py ──> assessment report
```

---

## Analyzer

`avd-analyze.py` turns a collected `avd-data-*` folder into a customer-ready
persona-mapping assessment. Pure Python 3.8+ standard library — no installs;
runs in Cloud Shell or on the architect's machine.

```bash
python3 avd-analyze.py <avd-data-folder>
# writes <folder>/analysis/report.md  and  persona_mapping.csv
```

It implements the Citrix/VDI sizing methodology adapted to AVD:

- **True event-based (sweep-line) concurrency** — peak concurrent *users*
  (per-user merged intervals) and *sessions* (per machine); never simple hourly
  counts. Full 0–23 hourly profile + daily peaks.
- **Persona classification** — Task Worker Light/Medium/Heavy from vCPU-per-user
  (`Machine_vCPU ÷ peak concurrency` over the top-10 machines: ≤0.5 / 0.51–1.19
  / ≥1.2); Knowledge Worker pooled (SingleSession+Random) / personal
  (SingleSession+Static); Power Worker (GPU SKU); Custom.
- **Usage & sizing** — per-user-per-day merged connected hours → average over
  *active* users → average of daily averages → × workdays for monthly demand;
  weekday/weekend split.
- **Non-exclusive personas**, multi-group overlap, shelfware (idle-host)
  detection, and an explicit assumptions section.

Output: a self-contained `report.md` (readable, section per stage) plus
`persona_mapping.csv` pre-filled to the Citrix Flex persona template.

## Roadmap

- **Cost-estimation layer** — map discovered VM SKUs to Azure retail pricing for
  a spend baseline (the collector already captures SKU + region).
- **Optional `.xlsx`** multi-sheet output (currently Markdown + CSV, stdlib only).
- **Multi-subscription collection** in a single run (collect currently targets
  one subscription via the env file).
