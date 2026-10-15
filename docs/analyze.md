---
layout: default
title: Analyze the Results
description: How avd-analyze.py turns collected AVD data into a persona-mapping report, and the calculations behind each section.
nav_order: 6
---

# Analyze the Results
{: .no_toc }

`avd-analyze.py` turns a collected `avd-data-*` folder into a customer-ready persona-mapping assessment. It is pure Python 3.8+ standard library — no installs — and runs anywhere the collected folder can be read (Cloud Shell or the architect's machine).

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## Run it

```bash
python3 avd-analyze.py <avd-data-folder>
# writes <folder>/analysis/report.md and persona_mapping.csv
```

Options:

```bash
python3 avd-analyze.py <avd-data-folder> -o <output-folder>    # custom output location
python3 avd-analyze.py <avd-data-folder> --workdays 22          # override monthly workday multiplier (default 30)
```

## Input data

The analyzer reads the CSVs and manifest written by [Phase 2](phase-2-collect.md): `hostpools.csv`, `sessionhosts.csv`, `appgroups.csv`, `sessions.csv`, and `manifest.json` (lookback window, hashed-usernames flag).

Sessions are deduplicated by `sessionID` and dropped if `startTime`/`endTime` is missing or invalid — a session that never reached `Connected` is excluded as a failed connect, not counted as usage.

## Concurrency — true time-overlap

Simple hourly counts overstate or understate real overlap, so the analyzer uses an event-based **sweep line**:

* Each session contributes a `+1` event at start and a `-1` event at end.
* Events are sorted so that an end is processed before a start at the same timestamp — a session ending exactly as another begins is never double-counted.
* Sweeping the sorted events produces constant-count segments, from which the analyzer derives:
  * **Peak concurrency** — the maximum segment count.
  * **Daily peaks** — peak concurrency computed separately per calendar day.
  * **Hourly profile** — a time-weighted average concurrency for each hour-of-day (0-23), used for the estate's hourly bar chart.

Before computing *user* concurrency, each user's own overlapping sessions are merged into single intervals, so one user with two overlapping sessions counts as one concurrent user, not two.

## Per-group analysis

For each desktop group (host pool), the analyzer computes:

* **Unique users** and **peak concurrent users** (merged intervals + sweep).
* **Peak concurrent sessions** per machine, a density signal.
* **vCPU per user** — for the ten most-loaded machines (by peak session concurrency), `Machine_vCPU ÷ peak_sessions_on_that_machine`, averaged across those ten. This is the key sizing metric feeding the Task Worker band.
* **Density** — peak concurrent users divided by VM count.

## Persona classification

Personas are assigned by rule, using pool configuration plus the vCPU-per-user metric:

| Condition | Persona |
|---|---|
| GPU-backed VM SKU (`Standard_N*`) | Power Worker |
| MultiSession + Random allocation | Task Worker Light / Medium / Heavy |
| SingleSession + Random allocation | Knowledge Worker (pooled) |
| SingleSession + Static allocation | Knowledge Worker (personal) |
| Anything else | Custom Worker |

Task Worker band thresholds (vCPU per user, averaged over the top-10 loaded machines):

* **Light** — 0.5 or below
* **Medium** — 0.51 to 1.19 (also the default when a pool has no usage data in the window)
* **Heavy** — 1.2 or above

## Usage and compute sizing

Usage hours are computed in a strict, non-inflating order:

1. Merge each user's overlapping sessions per day into connected hours.
2. Average those per-user hours across **active users only** for that day (idle users don't drag the average down).
3. Average the daily averages to get a weekday figure and a weekend figure.
4. **Monthly hours per user** = daily average x `--workdays` (default 30).
5. **Monthly demand (hours)** = monthly hours per user x unique users.

## Rollups and report sections

* Groups sharing the same persona are aggregated into one **persona rollup** row (users, peak concurrency, VMs, monthly hours/demand). Personas are **non-exclusive** — a user active in two pools counts in both.
* `report.md` assembles these into sections: Summary, Persona Mapping (per group), Persona Summary (rollup), Users & Overlap, Concurrency, Hourly Profile, Usage & Compute Sizing, Insights & Recommendations (busiest pool, idle/shelfware hosts, right-sizing suggestions), and Assumptions & Method.
* `persona_mapping.csv` is written from the same per-group data, pre-filled to the Citrix Flex persona template.

## Assumptions baked into the report

* All timestamps are treated as UTC.
* Invalid or failed-connect sessions are excluded before any calculation.
* A pool with zero usage in the lookback window defaults to the Medium Task Worker band — the report flags this so you can reclassify from configuration or a longer lookback before proposing.
* Monthly projections use a fixed workdays-per-month constant (`--workdays`, default 30).
* Where a VM's vCPU can't be resolved, vCPU/user is shown as `n/a` rather than guessed.

---

Next: [Clean Up](cleanup.md)
