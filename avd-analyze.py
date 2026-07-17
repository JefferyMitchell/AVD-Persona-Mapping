#!/usr/bin/env python3
"""
AVD Persona Mapping - Analyzer
==============================
Turns the data collected by avd-collect.sh into a customer-ready persona-mapping
assessment: true time-overlap concurrency, session-merged usage, vCPU-per-user
persona classification, and a report you can read straight into the Citrix Flex
persona spreadsheet.

Pure standard library (Python 3.8+). No pip installs. Runs anywhere the collected
folder can be read.

Usage:
    python3 avd-analyze.py <avd-data-folder> [-o <output-folder>] [--workdays 30]

Outputs (written to the output folder, default: <folder>/analysis):
    report.md            Readable assessment, section per methodology stage
    persona_mapping.csv  Pre-filled Citrix Flex persona template (one row/persona)

Methodology mirrors the team's Citrix/VDI analysis prompt, adapted to AVD:
    - True event-based concurrency (sweep line), never simple hourly counts
    - Session merge (per user) before usage totals
    - Usage: per-user-per-day connected time -> avg over ACTIVE users -> avg of
      daily averages -> x N workdays for monthly
    - Personas are non-exclusive (a user in two pools counts in both)
    - Task Worker density via vCPU-per-user = Machine_vCPU / peak concurrency
"""

import argparse
import csv
import os
import re
import statistics
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d+))?(.*)$")


def parse_ts(value):
    """Parse an ISO-8601 timestamp robustly across Python versions.

    Handles a trailing 'Z' and fractional seconds of any length (fromisoformat
    only accepts 3 or 6 digits before 3.11), returning a tz-aware datetime.
    """
    s = (value or "").strip().strip('"')
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    m = _TS_RE.match(s)
    if not m:
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            return None
    base, frac, rest = m.group(1), m.group(2) or "", m.group(3) or ""
    frac6 = (frac + "000000")[:6]
    if not rest:
        rest = "+00:00"
    try:
        return datetime.fromisoformat(f"{base}.{frac6}{rest}")
    except ValueError:
        return None


def short_host(name):
    """Normalise a session-host name to its short form for joining."""
    if not name:
        return ""
    return name.split("/")[-1].split(".")[0].lower()


def read_csv(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def find_one(folder, pattern):
    rx = re.compile(pattern)
    for fn in sorted(os.listdir(folder)):
        if rx.match(fn):
            return os.path.join(folder, fn)
    return None


# --------------------------------------------------------------------------- #
# Concurrency (true time-overlap, sweep line)
# --------------------------------------------------------------------------- #

def merge_intervals(intervals):
    """Union a list of (start, end) datetimes into non-overlapping intervals."""
    ivs = sorted((s, e) for s, e in intervals if s and e and e > s)
    if not ivs:
        return []
    merged = [list(ivs[0])]
    for s, e in ivs[1:]:
        if s <= merged[-1][1]:
            if e > merged[-1][1]:
                merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def sweep_segments(intervals):
    """Sweep intervals -> list of (seg_start, seg_end, count) constant segments.

    Ends are processed before starts at equal timestamps, so a session ending
    exactly as another starts is not counted as overlap.
    """
    events = []
    for s, e in intervals:
        if s and e and e > s:
            events.append((s, 1))
            events.append((e, -1))
    if not events:
        return []
    events.sort(key=lambda x: (x[0], x[1]))  # -1 (end) before +1 (start) at ties
    segments = []
    count = 0
    prev_t = events[0][0]
    for t, delta in events:
        if t > prev_t and count > 0:
            segments.append((prev_t, t, count))
        count += delta
        prev_t = t
    return segments


def peak_concurrency(intervals):
    segs = sweep_segments(intervals)
    return max((c for _, _, c in segs), default=0)


def hour_of_day_average(segments):
    """Time-weighted average concurrency for each hour-of-day slot (0-23)."""
    num = defaultdict(float)   # sum(count * seconds)
    den = defaultdict(float)   # sum(seconds observed in that hour slot)
    for seg_start, seg_end, count in segments:
        t = seg_start
        while t < seg_end:
            slot_end = (t.replace(minute=0, second=0, microsecond=0)
                        + timedelta(hours=1))
            chunk_end = min(slot_end, seg_end)
            dur = (chunk_end - t).total_seconds()
            num[t.hour] += count * dur
            den[t.hour] += dur
            t = chunk_end
    return {h: (num[h] / den[h] if den[h] else 0.0) for h in range(24)}


def daily_peaks(intervals):
    """Peak concurrency per calendar day."""
    by_day = defaultdict(list)
    for s, e in intervals:
        if s and e and e > s:
            by_day[s.date()].append((s, e))
    return {d: peak_concurrency(ivs) for d, ivs in by_day.items()}


# --------------------------------------------------------------------------- #
# Load + shape data
# --------------------------------------------------------------------------- #

class Data:
    def __init__(self, folder):
        self.folder = folder
        self.hostpools = read_csv(os.path.join(folder, "hostpools.csv"))
        self.sessionhosts = read_csv(os.path.join(folder, "sessionhosts.csv"))
        self.appgroups = read_csv(os.path.join(folder, "appgroups.csv"))
        self.sessions_raw = read_csv(os.path.join(folder, "sessions.csv"))

        # host pool lookup
        self.pool = {r["hostPoolName"]: r for r in self.hostpools}

        # machine -> vCPU / memory / os (keyed by short host name)
        self.machine = {}
        self.pool_machines = defaultdict(list)
        for r in self.sessionhosts:
            key = short_host(r.get("sessionHostName", ""))
            self.machine[key] = r
            self.pool_machines[r.get("hostPoolName", "")].append(r)

        # sessions: parse + dedup by sessionID + drop invalid
        seen = set()
        self.sessions = []
        for r in self.sessions_raw:
            sid = r.get("sessionID", "")
            if sid in seen:
                continue
            seen.add(sid)
            s = parse_ts(r.get("startTime"))
            e = parse_ts(r.get("endTime"))
            if not s or not e or e <= s:
                continue
            self.sessions.append({
                "user": r.get("userID", ""),
                "sid": sid,
                "machine": short_host(r.get("machineID", "")),
                "group": r.get("desktopGroup", ""),
                "start": s,
                "end": e,
            })

    def vcpu_of(self, machine_short):
        r = self.machine.get(machine_short)
        if not r:
            return None
        try:
            return float(r.get("vCPU") or "")
        except ValueError:
            return None


# --------------------------------------------------------------------------- #
# Persona classification
# --------------------------------------------------------------------------- #

CITRIX_STD = {
    "Task Worker Light": "8 vCPU, 32 GB RAM (16 sessions/VM)",
    "Task Worker Medium": "8 vCPU, 32 GB RAM (8 sessions/VM)",
    "Task Worker Heavy": "8 vCPU, 32 GB RAM (4 sessions/VM)",
    "Knowledge Worker (pooled)": "Pooled desktop",
    "Knowledge Worker (personal)": "4 vCPU, 16 GB RAM (personal/persistent)",
    "Power Worker": "6 vCPU, 55 GB RAM (GPU-backed)",
    "Custom Worker": "Customer specific",
}

DELIVERY = {
    "Task Worker Light": "Shared desktops (browser/low density)",
    "Task Worker Medium": "Shared desktops",
    "Task Worker Heavy": "Shared desktops",
    "Knowledge Worker (pooled)": "Pooled desktop",
    "Knowledge Worker (personal)": "Personal/persistent desktop",
    "Power Worker": "GPU-backed desktops",
    "Custom Worker": "Customer specific",
}


def classify_task_band(vcpu_per_user):
    if vcpu_per_user is None:
        return "Task Worker Medium"  # default when vCPU unknown
    if vcpu_per_user <= 0.5:
        return "Task Worker Light"
    if vcpu_per_user < 1.2:
        return "Task Worker Medium"
    return "Task Worker Heavy"


def is_gpu(vm_size):
    return bool(re.search(r"standard_n", (vm_size or "").lower()))


# --------------------------------------------------------------------------- #
# Analysis
# --------------------------------------------------------------------------- #

def analyse_group(d, group):
    pool = d.pool.get(group, {})
    gs = [s for s in d.sessions if s["group"] == group]
    machines = d.pool_machines.get(group, [])
    vm_count = len(machines)

    # unique users
    users = sorted({s["user"] for s in gs})

    # per-user merged active intervals (in this group) -> peak concurrent USERS
    user_ivs = []
    for u in users:
        ivs = merge_intervals([(s["start"], s["end"]) for s in gs if s["user"] == u])
        user_ivs.extend(ivs)
    peak_users = peak_concurrency(user_ivs)

    # per-machine peak concurrent SESSIONS (density) + vCPU-per-user
    machine_peaks = {}
    for m in machines:
        mkey = short_host(m.get("sessionHostName", ""))
        msess = [(s["start"], s["end"]) for s in gs if s["machine"] == mkey]
        machine_peaks[mkey] = peak_concurrency(msess)

    # sizing sample: top 10 machines by peak session concurrency (that had load)
    loaded = [(mk, pk) for mk, pk in machine_peaks.items() if pk > 0]
    loaded.sort(key=lambda x: x[1], reverse=True)
    top = loaded[:10]
    vcpu_per_user_vals = []
    for mk, pk in top:
        v = d.vcpu_of(mk)
        if v and pk > 0:
            vcpu_per_user_vals.append(v / pk)
    avg_vcpu_per_user = (statistics.mean(vcpu_per_user_vals)
                         if vcpu_per_user_vals else None)

    # persona
    support = (pool.get("sessionSupport") or "").strip()
    alloc = (pool.get("allocationType") or "").strip()
    sample_vm = machines[0] if machines else {}
    gpu = any(is_gpu(m.get("vmSize")) for m in machines)
    if gpu:
        persona = "Power Worker"
    elif support == "MultiSession" and alloc == "Random":
        persona = classify_task_band(avg_vcpu_per_user)
    elif support == "SingleSession" and alloc == "Random":
        persona = "Knowledge Worker (pooled)"
    elif support == "SingleSession" and alloc == "Static":
        persona = "Knowledge Worker (personal)"
    else:
        persona = "Custom Worker"

    # density: peak concurrent users per VM
    density = (peak_users / vm_count) if vm_count else 0.0

    return {
        "group": group,
        "persona": persona,
        "pool": pool,
        "sample_vm": sample_vm,
        "vm_count": vm_count,
        "unique_users": len(users),
        "users": users,
        "peak_users": peak_users,
        "peak_sessions": peak_concurrency([(s["start"], s["end"]) for s in gs]),
        "machine_peaks": machine_peaks,
        "top_machines": top,
        "avg_vcpu_per_user": avg_vcpu_per_user,
        "density": density,
        "sessions": gs,
    }


def usage_by_scope(sessions, scope_key):
    """Strict usage: per-user-per-day merged connected hours -> avg over ACTIVE
    users per day -> avg of daily averages. Split weekday/weekend. Returns dict
    scope -> {'weekday': hrs, 'weekend': hrs, 'overall': hrs}."""
    # scope -> day -> user -> [intervals]
    tree = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    for s in sessions:
        scope = scope_key(s)
        day = s["start"].date()
        tree[scope][day][s["user"]].append((s["start"], s["end"]))

    out = {}
    for scope, days in tree.items():
        wd, we = [], []
        for day, users in days.items():
            per_user_hours = []
            for u, ivs in users.items():
                secs = sum((e - st).total_seconds() for st, e in merge_intervals(ivs))
                per_user_hours.append(secs / 3600.0)
            if not per_user_hours:
                continue
            day_avg = statistics.mean(per_user_hours)   # active users only
            (we if day.weekday() >= 5 else wd).append(day_avg)
        out[scope] = {
            "weekday": statistics.mean(wd) if wd else 0.0,
            "weekend": statistics.mean(we) if we else 0.0,
            "overall": statistics.mean(wd + we) if (wd or we) else 0.0,
        }
    return out


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

def fmt(x, nd=2):
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:.{nd}f}"
    return str(x)


def build_report(d, groups, usage_group, usage_persona, args):
    L = []
    w = L.append
    total_users = len({s["user"] for s in d.sessions})
    span_days = 0
    if d.sessions:
        span_days = (max(s["end"] for s in d.sessions).date()
                     - min(s["start"] for s in d.sessions).date()).days + 1

    w("# AVD Persona Mapping — Assessment Report\n")
    w(f"_Source: `{os.path.basename(os.path.normpath(d.folder))}` · "
      f"{len(d.sessions)} sessions over ~{span_days} day(s) · "
      f"generated by avd-analyze.py_\n")

    # --- Summary ---
    w("## 1. Summary\n")
    w(f"- Host pools (desktop groups): **{len(groups)}**")
    w(f"- Session-host VMs: **{sum(g['vm_count'] for g in groups)}**")
    w(f"- Unique users (total, non-exclusive across pools): **{total_users}**")
    w(f"- Sessions analysed: **{len(d.sessions)}**")
    peak_all = peak_concurrency([(s["start"], s["end"]) for s in d.sessions])
    w(f"- Estate peak concurrent sessions (true overlap): **{peak_all}**\n")

    # --- Persona summary table (maps to the Flex spreadsheet) ---
    w("## 2. Persona Mapping (read this into the Flex spreadsheet)\n")
    w("| Desktop Group | Persona | OS | Current VM | vCPU/user | Peak concurrent users | Unique users | VMs | Density (users/VM) | Alloc |")
    w("|---|---|---|---|---|---|---|---|---|---|")
    for g in groups:
        vm = g["sample_vm"]
        os_desc = f"{vm.get('osType','?')}/{vm.get('imageSku','?')}"
        cur_vm = f"{vm.get('vmSize','?')} ({vm.get('vCPU','?')}vCPU/{vm.get('memoryGB','?')}GB)"
        alloc = g["pool"].get("allocationType", "?")
        persist = "Persistent" if alloc == "Static" else "Non-persistent"
        w(f"| {g['group']} | {g['persona']} | {os_desc} | {cur_vm} | "
          f"{fmt(g['avg_vcpu_per_user'])} | {g['peak_users']} | {g['unique_users']} | "
          f"{g['vm_count']} | {fmt(g['density'])} | {alloc} ({persist}) |")
    w("")

    # --- Users per group + overlap ---
    w("## 3. Users & Overlap\n")
    user_groups = defaultdict(set)
    for s in d.sessions:
        user_groups[s["user"]].add(s["group"])
    dist = defaultdict(int)
    for u, gset in user_groups.items():
        dist[min(len(gset), 3)] += 1
    w(f"- Users in exactly 1 group: **{dist[1]}**")
    w(f"- Users in exactly 2 groups: **{dist[2]}**")
    w(f"- Users in 3+ groups: **{dist[3]}**")
    multi = sorted(((u, len(g)) for u, g in user_groups.items() if len(g) > 1),
                   key=lambda x: x[1], reverse=True)[:10]
    if multi:
        w("- Top multi-group users: " + ", ".join(f"{u} ({n})" for u, n in multi))
    w("")
    w("| Desktop Group | Unique users |")
    w("|---|---|")
    for g in groups:
        w(f"| {g['group']} | {g['unique_users']} |")
    w("")

    # --- Concurrency detail ---
    w("## 4. Concurrency (true time-overlap)\n")
    for g in groups:
        w(f"### {g['group']} — {g['persona']}")
        w(f"- Peak concurrent **users**: **{g['peak_users']}** · "
          f"peak concurrent **sessions**: **{g['peak_sessions']}**")
        # top machines
        if g["top_machines"]:
            w("- Top machines by peak session concurrency:")
            for mk, pk in g["top_machines"]:
                vcpu = d.vcpu_of(mk)
                per = f" · {vcpu/pk:.2f} vCPU/user" if (vcpu and pk) else ""
                w(f"    - `{mk}`: {pk} concurrent{per}")
        # hourly
        segs = sweep_segments([(s["start"], s["end"]) for s in g["sessions"]])
        hourly = hour_of_day_average(segs)
        active_hours = {h: v for h, v in hourly.items() if v > 0}
        if active_hours:
            peak_h = max(active_hours, key=active_hours.get)
            w(f"- Busiest hour (UTC): **{peak_h:02d}:00** "
              f"(avg {active_hours[peak_h]:.2f} concurrent)")
        # daily
        dp = daily_peaks([(s["start"], s["end"]) for s in g["sessions"]])
        if dp:
            vals = list(dp.values())
            w(f"- Daily peak concurrency — min {min(vals)}, "
              f"median {statistics.median(vals):.1f}, max {max(vals)} "
              f"(across {len(vals)} active day(s))")
        w("")

    # --- Usage ---
    w("## 5. Usage & Compute Sizing\n")
    w("_Method: per-user-per-day connected hours (overlaps merged) → average "
      "across active users only → average of daily averages. Monthly = "
      f"daily average × {args.workdays}._\n")
    w("| Desktop Group | Persona | Avg hrs/user/day (wk) | (wknd) | Monthly hrs/user | Est. monthly demand (hrs) |")
    w("|---|---|---|---|---|---|")
    for g in groups:
        u = usage_group.get(g["group"], {})
        wk = u.get("weekday", 0.0)
        we = u.get("weekend", 0.0)
        monthly = u.get("overall", 0.0) * args.workdays
        demand = monthly * g["unique_users"]
        w(f"| {g['group']} | {g['persona']} | {fmt(wk)} | {fmt(we)} | "
          f"{fmt(monthly)} | {fmt(demand)} |")
    w("")

    # --- Insights ---
    w("## 6. Insights & Recommendations\n")
    busiest = max(groups, key=lambda g: g["peak_users"], default=None)
    if busiest:
        w(f"- **Peak demand driver:** `{busiest['group']}` "
          f"({busiest['persona']}) at {busiest['peak_users']} concurrent users.")
    shelf = [g for g in groups if g["unique_users"] == 0]
    for g in groups:
        idle_hosts = [mk for mk, pk in g["machine_peaks"].items() if pk == 0]
        if idle_hosts:
            w(f"- **Shelfware / under-utilised:** `{g['group']}` has "
              f"{len(idle_hosts)} host(s) with zero peak concurrency "
              f"({', '.join(idle_hosts)}).")
    for g in groups:
        if g["persona"].startswith("Task Worker") and g["avg_vcpu_per_user"]:
            w(f"- **Right-sizing (`{g['group']}`):** observed "
              f"{g['avg_vcpu_per_user']:.2f} vCPU/user → **{g['persona']}**; "
              f"map to Citrix std {CITRIX_STD[g['persona']]}.")
    w("- Personas are non-exclusive; multi-group users appear in each pool's totals.")
    w("- Region(s): " + ", ".join(sorted({g['pool'].get('region','?') for g in groups})) + ".")
    w("")
    return "\n".join(L)


TEMPLATE_HEADER = [
    "Persona", "Citrix Std config", "Customer specific VM sizing",
    "Customer specific persona names", "Delivery Mechanism",
    "No. of Unique Users", "No. of concurrent users", "OS",
    "No. of VMs running those personas", "Currently Persistent/Non-persistent",
    "Future (Persistent/Non-persistent)", "Avg duration of user logons/day",
    "AVG. Density/server (users/vm)", "Any other Comment",
]


def write_template_csv(path, d, groups, usage_group):
    with open(path, "w", newline="", encoding="utf-8") as f:
        wr = csv.writer(f)
        wr.writerow(TEMPLATE_HEADER)
        for g in groups:
            vm = g["sample_vm"]
            alloc = g["pool"].get("allocationType", "")
            persist = "Persistent" if alloc == "Static" else "Non-persistent"
            os_desc = f"{vm.get('osType','')} / {vm.get('imageSku','')}"
            cur_vm = (f"{vm.get('vmSize','')} "
                      f"({vm.get('vCPU','')} vCPU, {vm.get('memoryGB','')} GB)")
            avg_hrs = usage_group.get(g["group"], {}).get("overall", 0.0)
            wr.writerow([
                g["persona"],
                CITRIX_STD.get(g["persona"], ""),
                cur_vm,
                "",                                   # customer persona name (human)
                DELIVERY.get(g["persona"], ""),
                g["unique_users"],
                g["peak_users"],
                os_desc,
                g["vm_count"],
                persist,
                "",                                   # future (proposal)
                f"{avg_hrs:.2f} h",
                f"{g['density']:.2f}",
                f"pool={g['group']}; region={g['pool'].get('region','')}",
            ])


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def persona_of_group(d, group_analyses, group):
    for g in group_analyses:
        if g["group"] == group:
            return g["persona"]
    return "Custom Worker"


def main():
    ap = argparse.ArgumentParser(description="Analyse collected AVD data into a persona-mapping assessment.")
    ap.add_argument("folder", help="avd-data-* folder produced by avd-collect.sh")
    ap.add_argument("-o", "--out", help="output folder (default: <folder>/analysis)")
    ap.add_argument("--workdays", type=int, default=30, help="days/month for monthly projection (default 30)")
    args = ap.parse_args()

    # Ensure UTF-8 console output (Windows defaults to cp1252). No-op elsewhere.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass

    if not os.path.isdir(args.folder):
        sys.exit(f"Not a folder: {args.folder}")
    d = Data(args.folder)
    if not d.hostpools:
        sys.exit("No hostpools.csv found — is this an avd-data folder?")

    groups = [analyse_group(d, r["hostPoolName"]) for r in d.hostpools]

    usage_group = usage_by_scope(d.sessions, lambda s: s["group"])
    persona_by_group = {g["group"]: g["persona"] for g in groups}
    usage_persona = usage_by_scope(d.sessions, lambda s: persona_by_group.get(s["group"], "Custom Worker"))

    out_dir = args.out or os.path.join(args.folder, "analysis")
    os.makedirs(out_dir, exist_ok=True)
    report = build_report(d, groups, usage_group, usage_persona, args)
    with open(os.path.join(out_dir, "report.md"), "w", encoding="utf-8") as f:
        f.write(report)
    write_template_csv(os.path.join(out_dir, "persona_mapping.csv"), d, groups, usage_group)

    print(report)
    print(f"\n[written] {os.path.join(out_dir, 'report.md')}")
    print(f"[written] {os.path.join(out_dir, 'persona_mapping.csv')}")


if __name__ == "__main__":
    main()
