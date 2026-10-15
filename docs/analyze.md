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

The analyzer reads the CSVs and manifest written by the collector: `hostpools.csv`, `sessionhosts.csv`, `appgroups.csv`, `sessions.csv`, and `manifest.json` (lookback window, hashed-usernames flag).

Two further files are optional and come from the AVD Insights performance counters in the `Perf` table. When present they switch the Task Worker band from an allocation estimate to a measurement and add two report sections:

* `hostperf.csv` — one row per host per hour: CPU average and p95, available memory average and minimum, C: disk latency, user input delay p95 and max, RemoteFX round-trip time.
* `hostapps.csv` — one row per host per process image name seen in the per-process input-delay counter, with the hours and session-days it was observed and its input delay.

Sessions are deduplicated by `sessionID` and dropped if `startTime`/`endTime` is missing or invalid — a session that never reached `Connected` is excluded as a failed connect, not counted as usage. Host names are joined on their short form (first DNS label, lower-cased), so `host01`, `host01.contoso.local` and `/…/sessionHosts/host01` all match.

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

Task Worker band thresholds (vCPU per user):

* **Light** — 0.5 or below
* **Medium** — 0.51 to 1.19 (also the default when a pool has no usage data in the window)
* **Heavy** — 1.2 or above

The vCPU-per-user figure fed to those thresholds depends on what data is available, and the report labels which was used:

| Basis | When | Figure |
|---|---|---|
| **measured** | `hostperf.csv` has data for the pool's hosts | Required vCPU/user (see [Host performance](#host-performance-avd-insights)) |
| **allocated** | sessions only | `Machine_vCPU ÷ peak_sessions` averaged over the top-10 loaded machines |
| **default** | no sessions in the window | Medium, flagged for manual review |

The difference matters. Allocation only says how many users shared a host. A host at 16 sessions and 30% CPU is Light; the same host pegged at 95% is Heavy. Allocation cannot tell them apart; measurement can, and the Insights section calls out any pool whose band changed.

## Host performance (AVD Insights)

Only **loaded hours** count: hours in which a host had at least one connected session (determined by overlapping `sessions.csv` with each hourly `hostperf.csv` row). Idle hours would otherwise dilute every average.

Per host:

* **Peak-hour CPU p95** — the p95 CPU averaged over the host's peak-concurrency hours.
* **Required vCPU/user** = `(peak-hour CPU p95 ÷ 80%) × host_vCPU ÷ peak_concurrent_sessions`. This is the allocation that would hold p95 CPU at an 80% target for the users actually observed, and is directly comparable to the Light/Medium/Heavy thresholds.
* **Consumed vCPU/user** = `(CPU avg ÷ 100) × host_vCPU ÷ concurrent_sessions`, averaged over loaded hours. The raw consumption figure, useful for the sizing narrative.
* **Memory/user** = `(host_RAM − minimum_available_MB) ÷ concurrent_sessions`, averaged over loaded hours. Includes OS and shared overhead, so it overstates per-user need on lightly loaded hosts.
* **Input delay p95** — median of the hourly p95 values over loaded hours. **Input delay max** — the maximum seen.
* **Disk latency** and **RTT** — plain averages over loaded hours.

Group values are the mean of the per-host values. Hosts are flagged individually as "under pressure" when peak-hour CPU p95 is 85% or higher, or input delay p95 is 150 ms or higher. Pools are flagged in the Insights section as:

* **Oversized** — peak-hour CPU p95 below 40%: room to raise density or step down the VM size.
* **Saturated** — peak-hour CPU p95 at or above 85%: users are CPU-bound at peak.
* **User experience** — median input delay p95 at or above 150 ms: users are likely noticing lag.

These thresholds are heuristics set as constants at the top of the performance section in the script.

## Application profile

`hostapps.csv` is aggregated per pool by process image name: session-days and active hours are summed across the pool's hosts, host count is how many hosts saw the process, and input delay p95 is the worst host. A fixed list of Windows shell and platform processes (`dwm`, `explorer`, `svchost`, `rdpclip`, the FSLogix and AVD agent services, and so on) is excluded. The top 12 by session-days are reported per pool.

This shows what ran with user focus, not CPU consumed per application. The per-process counter only samples processes that had input, so background services never appear.

## Usage and compute sizing

Usage hours are computed in a strict, non-inflating order:

1. Merge each user's overlapping sessions per day into connected hours.
2. Average those per-user hours across **active users only** for that day (idle users don't drag the average down).
3. Average the daily averages to get a weekday figure and a weekend figure.
4. **Monthly hours per user** = daily average x `--workdays` (default 30).
5. **Monthly demand (hours)** = monthly hours per user x unique users.

## Rollups and report sections

* Groups sharing the same persona are aggregated into one **persona rollup** row (users, peak concurrency, VMs, monthly hours/demand). Personas are **non-exclusive** — a user active in two pools counts in both.
* `report.md` assembles these into sections: Summary, Persona Mapping (per group, with both allocated and measured vCPU/user), Persona Summary (rollup), Users & Overlap, Concurrency, Hourly Profile, Usage & Compute Sizing, Host Performance, Application Profile, Insights & Recommendations (busiest pool, idle/shelfware hosts, right-sizing, oversized/saturated/laggy pools, bands changed by measurement), and Assumptions & Method. `report.html` is the same content, self-contained.
* `persona_mapping.csv` is written from the same per-group data, pre-filled to the Citrix Flex persona template. The comment column carries the measured vCPU/user and the top five applications when available.

## Assumptions baked into the report

* All timestamps are treated as UTC.
* Invalid or failed-connect sessions are excluded before any calculation.
* A pool with zero usage in the lookback window defaults to the Medium Task Worker band — the report flags this so you can reclassify from configuration or a longer lookback before proposing.
* Monthly projections use a fixed workdays-per-month constant (`--workdays`, default 30).
* Where a VM's vCPU can't be resolved, vCPU/user is shown as `n/a` rather than guessed.
* Performance metrics use loaded hours only, target 80% CPU, and the 40% / 85% / 150 ms thresholds above. A pool whose hosts have no performance rows falls back to the allocated band and the report says so.
* Memory/user includes OS and shared overhead; treat it as an upper bound.

---

Next: [Clean Up](cleanup.md)
