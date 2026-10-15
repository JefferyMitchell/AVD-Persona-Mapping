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
    - When AVD Insights performance counters are present (hostperf.csv), the
      Task Worker band is instead MEASURED: the vCPU per user required to hold
      p95 CPU at 80% during each host's peak-concurrency hours. Memory per
      user, input delay, disk latency and RTT are reported alongside.
    - hostapps.csv (per-process input-delay counter) gives an application
      profile per desktop group with no agent and no user identifiers.
"""

import argparse
import csv
import html
import json
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


def _num(v):
    """Float or None for a CSV cell (blank cells are None)."""
    if v is None:
        return None
    v = str(v).strip()
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
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
        self.manifest = {}
        mpath = os.path.join(folder, "manifest.json")
        if os.path.exists(mpath):
            try:
                with open(mpath, encoding="utf-8") as f:
                    self.manifest = json.load(f)
            except (ValueError, OSError):
                self.manifest = {}

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

        # hourly host performance (AVD Insights counters), keyed by short host
        self.perf = defaultdict(list)
        for r in read_csv(os.path.join(folder, "hostperf.csv")):
            h = parse_ts(r.get("hour"))
            key = short_host(r.get("machineID", ""))
            if not h or not key:
                continue
            row = {f: _num(r.get(f)) for f in PERF_FIELDS}
            row["hour"] = h
            self.perf[key].append(row)
        self.has_perf = bool(self.perf)

        # application inventory per host (process image names only)
        self.apps = defaultdict(list)
        for r in read_csv(os.path.join(folder, "hostapps.csv")):
            key = short_host(r.get("machineID", ""))
            proc = (r.get("process") or "").strip().lower()
            if not key or not proc:
                continue
            self.apps[key].append({
                "process": proc,
                "activeHours": _num(r.get("activeHours")) or 0.0,
                "sessionDays": _num(r.get("sessionDays")) or 0.0,
                "delayAvg": _num(r.get("inputDelayAvgMs")),
                "delayP95": _num(r.get("inputDelayP95Ms")),
            })
        self.has_apps = bool(self.apps)

    def vcpu_of(self, machine_short):
        r = self.machine.get(machine_short)
        if not r:
            return None
        try:
            return float(r.get("vCPU") or "")
        except ValueError:
            return None

    def mem_gb_of(self, machine_short):
        r = self.machine.get(machine_short)
        return _num(r.get("memoryGB")) if r else None


PERF_FIELDS = ["cpuAvgPct", "cpuP95Pct", "memAvailAvgMB", "memAvailMinMB",
               "diskLatencyAvgMs", "inputDelayP95Ms", "inputDelayMaxMs",
               "rttAvgMs", "samples"]


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
# Performance (AVD Insights counters) and application profile
# --------------------------------------------------------------------------- #

CPU_TARGET_PCT = 80.0        # sizing target: hold p95 CPU at or below this
CPU_OVERSIZED_PCT = 40.0     # peak-hour p95 below this -> host is oversized
CPU_SATURATED_PCT = 85.0     # peak-hour p95 above this -> host is saturated
INPUT_DELAY_WARN_MS = 150.0  # p95 input delay above this -> users notice lag

# Windows shell / platform processes that appear in the per-process counter
# on every session but say nothing about the user's workload.
NOISE_PROCESSES = {
    "dwm", "explorer", "svchost", "csrss", "winlogon", "sihost", "taskhostw",
    "runtimebroker", "searchhost", "searchui", "searchapp", "searchindexer",
    "startmenuexperiencehost", "shellexperiencehost", "textinputhost",
    "ctfmon", "rdpclip", "lockapp", "applicationframehost", "systemsettings",
    "conhost", "fontdrvhost", "backgroundtaskhost", "msedgewebview2",
    "smartscreen", "securityhealthsystray", "userinit", "logonui", "wwahost",
    "dllhost", "taskmgr", "wermgr", "werfault", "rdpinit", "rdpshell",
    "rdpsa", "rdpsaproxy", "widgets", "phoneexperiencehost", "yourphone",
    "screenclippinghost", "useroobebroker", "msiexec", "wuauclt",
    "frxtray", "frxsvc", "frxccds", "rdagent", "rdagentbootloader",
    "remotedesktopagent", "windowsazureguestagent", "waappagent",
}


def perf_for_group(d, machines, gs):
    """Measured host metrics for one desktop group.

    For each host with Insights data, only hours in which the host had at
    least one connected session ('loaded hours') are considered. Returns a
    dict of group means plus per-machine detail, or None if no host in the
    group has any loaded hour with performance data.
    """
    per_machine = {}
    for m in machines:
        mk = short_host(m.get("sessionHostName", ""))
        rows = d.perf.get(mk)
        if not rows:
            continue
        vcpu = d.vcpu_of(mk)
        mem_mb = (d.mem_gb_of(mk) or 0.0) * 1024.0
        msess = [(s["start"], s["end"]) for s in gs if s["machine"] == mk]
        loaded = []
        for r in rows:
            h0 = r["hour"]
            h1 = h0 + timedelta(hours=1)
            conc = sum(1 for s, e in msess if s < h1 and e > h0)
            if conc > 0:
                loaded.append((conc, r))
        if not loaded:
            continue
        peak_conc = max(c for c, _ in loaded)
        peak_cpu = [r["cpuP95Pct"] for c, r in loaded
                    if c == peak_conc and r["cpuP95Pct"] is not None]
        cpu_peak = statistics.mean(peak_cpu) if peak_cpu else None
        need = ((cpu_peak / CPU_TARGET_PCT) * vcpu / peak_conc
                if (vcpu and cpu_peak is not None) else None)
        busy = [r["cpuP95Pct"] for _, r in loaded if r["cpuP95Pct"] is not None]
        consumed = [(r["cpuAvgPct"] / 100.0) * vcpu / c for c, r in loaded
                    if vcpu and r["cpuAvgPct"] is not None]
        mem_user = [(mem_mb - r["memAvailMinMB"]) / c for c, r in loaded
                    if mem_mb and r["memAvailMinMB"] is not None
                    and mem_mb > r["memAvailMinMB"]]
        delay95 = [r["inputDelayP95Ms"] for _, r in loaded if r["inputDelayP95Ms"] is not None]
        delaymax = [r["inputDelayMaxMs"] for _, r in loaded if r["inputDelayMaxMs"] is not None]
        disk = [r["diskLatencyAvgMs"] for _, r in loaded if r["diskLatencyAvgMs"] is not None]
        rtt = [r["rttAvgMs"] for _, r in loaded if r["rttAvgMs"] is not None]
        per_machine[mk] = {
            "loaded_hours": len(loaded),
            "peak_conc": peak_conc,
            "cpu_busy_p95": statistics.mean(busy) if busy else None,
            "cpu_peak_p95": cpu_peak,
            "need_vcpu_user": need,
            "consumed_vcpu_user": statistics.mean(consumed) if consumed else None,
            "mem_mb_user": statistics.mean(mem_user) if mem_user else None,
            "delay_p95": statistics.median(delay95) if delay95 else None,
            "delay_max": max(delaymax) if delaymax else None,
            "disk_ms": statistics.mean(disk) if disk else None,
            "rtt_ms": statistics.mean(rtt) if rtt else None,
        }
    if not per_machine:
        return None

    def mean_of(k):
        vals = [v[k] for v in per_machine.values() if v[k] is not None]
        return statistics.mean(vals) if vals else None

    g = {k: mean_of(k) for k in ("cpu_busy_p95", "cpu_peak_p95", "need_vcpu_user",
                                 "consumed_vcpu_user", "mem_mb_user", "delay_p95",
                                 "disk_ms", "rtt_ms")}
    g["delay_max"] = max((v["delay_max"] for v in per_machine.values()
                          if v["delay_max"] is not None), default=None)
    g["hosts"] = len(per_machine)
    g["per_machine"] = per_machine
    return g


def apps_for_group(d, machines, top_n=12):
    """Aggregate the per-host application inventory for one group."""
    agg = defaultdict(lambda: {"hours": 0.0, "days": 0.0, "hosts": 0, "delay": []})
    for m in machines:
        mk = short_host(m.get("sessionHostName", ""))
        for a in d.apps.get(mk, []):
            p = a["process"]
            if p in NOISE_PROCESSES:
                continue
            agg[p]["hours"] += a["activeHours"]
            agg[p]["days"] += a["sessionDays"]
            agg[p]["hosts"] += 1
            if a["delayP95"] is not None:
                agg[p]["delay"].append(a["delayP95"])
    rows = [{"process": p, "hours": v["hours"], "days": v["days"],
             "hosts": v["hosts"],
             "delay_p95": max(v["delay"]) if v["delay"] else None}
            for p, v in agg.items()]
    rows.sort(key=lambda r: (r["days"], r["hours"]), reverse=True)
    return rows[:top_n]


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

    # measured performance (AVD Insights) and application profile
    perf = perf_for_group(d, machines, gs) if d.has_perf else None
    measured = perf["need_vcpu_user"] if perf else None
    apps = apps_for_group(d, machines) if d.has_apps else []

    # persona
    support = (pool.get("sessionSupport") or "").strip()
    alloc = (pool.get("allocationType") or "").strip()
    sample_vm = machines[0] if machines else {}
    gpu = any(is_gpu(m.get("vmSize")) for m in machines)
    band_basis = None
    if gpu:
        persona = "Power Worker"
    elif support == "MultiSession" and alloc == "Random":
        if measured is not None:
            persona, band_basis = classify_task_band(measured), "measured"
        elif avg_vcpu_per_user is not None:
            persona, band_basis = classify_task_band(avg_vcpu_per_user), "allocated"
        else:
            persona, band_basis = classify_task_band(None), "default"
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
        "band_basis": band_basis,
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
        "measured_vcpu_per_user": measured,
        "perf": perf,
        "apps": apps,
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


def persona_rollup(groups, usage_persona, workdays):
    """Aggregate the per-group analyses up to one row per persona."""
    by = defaultdict(list)
    for g in groups:
        by[g["persona"]].append(g)
    rows = []
    for persona, gs in sorted(by.items()):
        all_sess = [s for g in gs for s in g["sessions"]]
        users = {s["user"] for s in all_sess}
        user_ivs = []
        for u in users:
            user_ivs.extend(merge_intervals(
                [(s["start"], s["end"]) for s in all_sess if s["user"] == u]))
        monthly = usage_persona.get(persona, {}).get("overall", 0.0) * workdays
        rows.append({
            "persona": persona,
            "groups": [g["group"] for g in gs],
            "unique_users": len(users),
            "peak_users": peak_concurrency(user_ivs),
            "vms": sum(g["vm_count"] for g in gs),
            "monthly_hrs_user": monthly,
            "monthly_demand": monthly * len(users),
        })
    return rows


def hourly_bar(value, maxv, width=24):
    if maxv <= 0:
        return ""
    return "█" * max(0, int(round(value / maxv * width)))


def build_report(d, groups, usage_group, usage_persona, args):
    L = []
    w = L.append
    total_users = len({s["user"] for s in d.sessions})
    span_days = 0
    if d.sessions:
        span_days = (max(s["end"] for s in d.sessions).date()
                     - min(s["start"] for s in d.sessions).date()).days + 1
    lookback = d.manifest.get("lookbackDays", "?")
    hashed = d.manifest.get("usernamesHashed", True)

    w("# AVD Persona Mapping — Assessment Report\n")
    w(f"_Source: `{os.path.basename(os.path.normpath(d.folder))}` · "
      f"{len(d.sessions)} connected sessions over ~{span_days} active day(s) · "
      f"{lookback}-day lookback · generated by avd-analyze.py_\n")

    # --- 1. Summary ---
    w("## 1. Summary\n")
    peak_all = peak_concurrency([(s["start"], s["end"]) for s in d.sessions])
    w(f"- Host pools (desktop groups): **{len(groups)}**")
    w(f"- Session-host VMs: **{sum(g['vm_count'] for g in groups)}**")
    w(f"- Unique users (total, non-exclusive across pools): **{total_users}**")
    w(f"- Connected sessions analysed: **{len(d.sessions)}**")
    w(f"- Estate peak concurrent sessions (true overlap): **{peak_all}**")
    idle_total = sum(1 for g in groups for pk in g["machine_peaks"].values() if pk == 0)
    if idle_total:
        w(f"- Hosts with zero observed concurrency (shelfware signal): **{idle_total}**")
    w("")

    # --- 2. Persona Mapping (per group) ---
    w("## 2. Persona Mapping (per desktop group)\n")
    w("| Desktop Group | Persona | OS | Current VM | Alloc vCPU/user | Measured vCPU/user | Peak concurrent users | Unique users | VMs | Density (users/VM) | Alloc |")
    w("|---|---|---|---|---|---|---|---|---|---|---|")
    for g in groups:
        vm = g["sample_vm"]
        os_desc = f"{vm.get('osType','?')}/{vm.get('imageSku','?')}"
        cur_vm = f"{vm.get('vmSize','?')} ({vm.get('vCPU','?')}vCPU/{vm.get('memoryGB','?')}GB)"
        alloc = g["pool"].get("allocationType", "?")
        persist = "Persistent" if alloc == "Static" else "Non-persistent"
        band = g["persona"]
        if g["band_basis"] == "default":
            band += " *(default — no usage)*"
        elif g["band_basis"] == "allocated" and d.has_perf:
            band += " *(allocated — no perf data for this pool)*"
        elif g["band_basis"] == "measured":
            band += " *(measured)*"
        w(f"| {g['group']} | {band} | {os_desc} | {cur_vm} | "
          f"{fmt(g['avg_vcpu_per_user'])} | {fmt(g['measured_vcpu_per_user'])} | "
          f"{g['peak_users']} | {g['unique_users']} | "
          f"{g['vm_count']} | {fmt(g['density'])} | {alloc} ({persist}) |")
    w("")

    # --- 3. Persona Summary (rollup) ---
    w("## 3. Persona Summary (rollup)\n")
    w("| Persona | Citrix std config | Pools | Unique users | Peak concurrent users | VMs | Monthly hrs/user | Monthly demand (hrs) |")
    w("|---|---|---|---|---|---|---|---|")
    for r in persona_rollup(groups, usage_persona, args.workdays):
        w(f"| {r['persona']} | {CITRIX_STD.get(r['persona'],'')} | "
          f"{len(r['groups'])} | {r['unique_users']} | {r['peak_users']} | "
          f"{r['vms']} | {fmt(r['monthly_hrs_user'])} | {fmt(r['monthly_demand'])} |")
    w("")

    # --- 4. Users & Overlap ---
    w("## 4. Users & Overlap\n")
    user_groups = defaultdict(set)
    for s in d.sessions:
        user_groups[s["user"]].add(s["group"])
    dist = defaultdict(int)
    for u, gset in user_groups.items():
        dist[min(len(gset), 3)] += 1
    w(f"- Users in exactly 1 group: **{dist[1]}** · 2 groups: **{dist[2]}** · 3+ groups: **{dist[3]}**")
    multi = sorted(((u, len(g)) for u, g in user_groups.items() if len(g) > 1),
                   key=lambda x: x[1], reverse=True)[:10]
    if multi:
        w("- Top multi-group users: " + ", ".join(f"`{u}` ({n})" for u, n in multi))
    w("")
    w("| Desktop Group | Unique users |")
    w("|---|---|")
    for g in groups:
        w(f"| {g['group']} | {g['unique_users']} |")
    w("")

    # --- 5. Concurrency (true time-overlap) ---
    w("## 5. Concurrency (true time-overlap)\n")
    for g in groups:
        w(f"### {g['group']} — {g['persona']}")
        w(f"- Peak concurrent **users**: **{g['peak_users']}** · "
          f"peak concurrent **sessions**: **{g['peak_sessions']}**")
        if g["top_machines"]:
            w("- Top machines by peak session concurrency:")
            for mk, pk in g["top_machines"]:
                vcpu = d.vcpu_of(mk)
                per = f" · {vcpu/pk:.2f} vCPU/user" if (vcpu and pk) else ""
                w(f"    - `{mk}`: {pk} concurrent{per}")
        dp = daily_peaks([(s["start"], s["end"]) for s in g["sessions"]])
        if dp:
            vals = list(dp.values())
            w(f"- Daily peak concurrency — min {min(vals)}, "
              f"median {statistics.median(vals):.1f}, max {max(vals)} "
              f"(across {len(vals)} active day(s))")
        w("")

    # --- 6. Hourly profile (estate, UTC) ---
    w("## 6. Hourly Concurrency Profile (estate, UTC)\n")
    segs_all = sweep_segments([(s["start"], s["end"]) for s in d.sessions])
    hourly = hour_of_day_average(segs_all)
    maxv = max(hourly.values()) if hourly else 0
    if maxv > 0:
        w("```")
        for h in range(24):
            w(f"{h:02d}:00  {hourly[h]:5.2f}  {hourly_bar(hourly[h], maxv)}")
        w("```")
        active = {h: v for h, v in hourly.items() if v > 0}
        peak_h = max(active, key=active.get)
        quiet_h = min(active, key=active.get)
        w(f"- Peak hour: **{peak_h:02d}:00 UTC** ({active[peak_h]:.2f} avg concurrent) · "
          f"quietest active hour: **{quiet_h:02d}:00 UTC** ({active[quiet_h]:.2f})")
        w(f"- Active window spans {min(active):02d}:00–{max(active):02d}:00 UTC.")
    else:
        w("_No usage telemetry available to profile._")
    w("")

    # --- 7. Usage & Compute Sizing ---
    w("## 7. Usage & Compute Sizing\n")
    w("_Method: per-user-per-day connected hours (overlaps merged) → average "
      "across active users only → average of daily averages. Monthly = "
      f"daily average × {args.workdays}._\n")
    w("| Desktop Group | Persona | Avg hrs/user/day (wk) | (wknd) | Monthly hrs/user | Est. monthly demand (hrs) |")
    w("|---|---|---|---|---|---|")
    for g in groups:
        u = usage_group.get(g["group"], {})
        monthly = u.get("overall", 0.0) * args.workdays
        w(f"| {g['group']} | {g['persona']} | {fmt(u.get('weekday',0.0))} | "
          f"{fmt(u.get('weekend',0.0))} | {fmt(monthly)} | "
          f"{fmt(monthly * g['unique_users'])} |")
    w("")

    # --- 8. Host Performance (AVD Insights counters) ---
    w("## 8. Host Performance (AVD Insights counters)\n")

    def pct(x, nd=1):
        return f"{x:.{nd}f}%" if x is not None else "n/a"

    if d.has_perf:
        w("_Loaded hours only (host had at least one connected session). "
          "'Required vCPU/user' is the vCPU per user needed to hold p95 CPU at "
          f"{CPU_TARGET_PCT:.0f}% during each host's peak-concurrency hours; it is "
          "the measured input to the Task Worker band._\n")
        w("| Desktop Group | Hosts w/ data | Busy-hour CPU p95 | Peak-hour CPU p95 | Consumed vCPU/user | Required vCPU/user | Memory/user (GB) | Input delay p95 (ms) | Disk latency (ms) | RTT (ms) |")
        w("|---|---|---|---|---|---|---|---|---|---|")
        for g in groups:
            p = g["perf"]
            if not p:
                w(f"| {g['group']} | 0 | n/a | n/a | n/a | n/a | n/a | n/a | n/a | n/a |")
                continue
            mem_gb = p["mem_mb_user"] / 1024.0 if p["mem_mb_user"] is not None else None
            w(f"| {g['group']} | {p['hosts']} | {pct(p['cpu_busy_p95'])} | "
              f"{pct(p['cpu_peak_p95'])} | {fmt(p['consumed_vcpu_user'])} | "
              f"{fmt(p['need_vcpu_user'])} | {fmt(mem_gb)} | {fmt(p['delay_p95'], 0)} | "
              f"{fmt(p['disk_ms'], 1)} | {fmt(p['rtt_ms'], 0)} |")
        w("")
        flagged = []
        for g in groups:
            if not g["perf"]:
                continue
            for mk, v in g["perf"]["per_machine"].items():
                reasons = []
                if v["cpu_peak_p95"] is not None and v["cpu_peak_p95"] >= CPU_SATURATED_PCT:
                    reasons.append(f"CPU p95 {v['cpu_peak_p95']:.0f}% at peak")
                if v["delay_p95"] is not None and v["delay_p95"] >= INPUT_DELAY_WARN_MS:
                    reasons.append(f"input delay p95 {v['delay_p95']:.0f} ms")
                if reasons:
                    flagged.append((g["group"], mk, v["peak_conc"], "; ".join(reasons)))
        if flagged:
            w("Hosts under pressure:")
            for grp, mk, pc, why in sorted(flagged):
                w(f"- `{mk}` ({grp}, peak {pc} session(s)): {why}")
            w("")
    else:
        w("_No performance counters in this dataset (AVD Insights not enabled, or no "
          "Perf data in the window). Task Worker bands use allocated vCPU per user._\n")

    # --- 9. Application Profile ---
    w("## 9. Application Profile (per desktop group)\n")
    if d.has_apps:
        w("_From the per-process input-delay counter: application image names with "
          "the session-days and active hours in which they were observed. Windows "
          "shell and platform processes are excluded. No user identifiers._\n")
        for g in groups:
            w(f"### {g['group']} — {g['persona']}")
            if not g["apps"]:
                w("_No application data for this pool._\n")
                continue
            w("| Application | Session-days | Active hours | Hosts | Input delay p95 (ms) |")
            w("|---|---|---|---|---|")
            for a in g["apps"]:
                w(f"| `{a['process']}` | {a['days']:.0f} | {a['hours']:.0f} | "
                  f"{a['hosts']} | {fmt(a['delay_p95'], 0)} |")
            w("")
    else:
        w("_No application data in this dataset._\n")

    # --- 10. Insights & Recommendations ---
    w("## 10. Insights & Recommendations\n")
    busiest = max(groups, key=lambda g: g["peak_users"], default=None)
    if busiest and busiest["peak_users"] > 0:
        w(f"- **Peak demand driver:** `{busiest['group']}` "
          f"({busiest['persona']}) at {busiest['peak_users']} concurrent users.")
    for g in groups:
        idle_hosts = [mk for mk, pk in g["machine_peaks"].items() if pk == 0]
        if idle_hosts:
            w(f"- **Shelfware / under-utilised:** `{g['group']}` has "
              f"{len(idle_hosts)} host(s) with zero peak concurrency "
              f"({', '.join(idle_hosts)}) — candidates to deallocate or reclaim.")
    for g in groups:
        if not g["persona"].startswith("Task Worker"):
            continue
        if g["measured_vcpu_per_user"] is not None:
            v, basis = g["measured_vcpu_per_user"], "measured"
        elif g["avg_vcpu_per_user"]:
            v, basis = g["avg_vcpu_per_user"], "allocated"
        else:
            continue
        w(f"- **Right-sizing (`{g['group']}`):** {basis} "
          f"{v:.2f} vCPU/user → **{g['persona']}**; "
          f"map to Citrix std {CITRIX_STD[g['persona']]}.")
    for g in groups:
        p = g["perf"]
        if not p:
            continue
        if p["cpu_peak_p95"] is not None and p["cpu_peak_p95"] < CPU_OVERSIZED_PCT:
            w(f"- **Oversized (`{g['group']}`):** peak-hour CPU p95 averages "
              f"{p['cpu_peak_p95']:.0f}% across {p['hosts']} host(s) — room to raise "
              "density or step down the VM size.")
        if p["cpu_peak_p95"] is not None and p["cpu_peak_p95"] >= CPU_SATURATED_PCT:
            w(f"- **Saturated (`{g['group']}`):** peak-hour CPU p95 averages "
              f"{p['cpu_peak_p95']:.0f}% — users are CPU-bound at peak; size the "
              "target up or lower density.")
        if p["delay_p95"] is not None and p["delay_p95"] >= INPUT_DELAY_WARN_MS:
            w(f"- **User experience (`{g['group']}`):** median input delay p95 is "
              f"{p['delay_p95']:.0f} ms (threshold {INPUT_DELAY_WARN_MS:.0f} ms) — "
              "users are likely noticing lag.")
        if g["band_basis"] == "measured" and g["avg_vcpu_per_user"] is not None:
            alloc_band = classify_task_band(g["avg_vcpu_per_user"])
            if alloc_band != g["persona"]:
                w(f"- **Band changed by measurement (`{g['group']}`):** allocation "
                  f"suggests {alloc_band} ({g['avg_vcpu_per_user']:.2f} vCPU/user) but "
                  f"measured load requires {g['measured_vcpu_per_user']:.2f} vCPU/user "
                  f"→ **{g['persona']}**.")
    w("- Personas are non-exclusive; multi-group users appear in each pool's totals.")
    w("- Region(s): " + ", ".join(sorted({g['pool'].get('region','?') for g in groups})) + ".")
    w("")

    # --- 11. Assumptions & Method ---
    w("## 11. Assumptions & Method\n")
    defaulted = [g["group"] for g in groups
                 if g["persona"].startswith("Task Worker") and g["avg_vcpu_per_user"] is None]
    w(f"- Timestamps are UTC. Lookback window: {lookback} days. "
      f"Usernames {'SHA-256 hashed' if hashed else 'RAW'} at collection.")
    w("- Concurrency is **true event-based overlap** (sweep line); at equal "
      "timestamps a session end is processed before a start, so back-to-back "
      "sessions are never double-counted.")
    w("- A session's window is first `Connected` to last event; sessions that "
      "never reached `Connected` (failed connects) are excluded as invalid.")
    w("- Task Worker band = mean(Machine_vCPU ÷ peak session concurrency) over "
      "the top-10 loaded machines: Light ≤0.5, Medium 0.51–1.19, Heavy ≥1.2 vCPU/user.")
    if defaulted:
        w(f"- **Assumption:** pool(s) {', '.join('`'+x+'`' for x in defaulted)} "
          "had no usage in the window, so the Task Worker band defaults to Medium — "
          "classify from configuration or a longer lookback before proposing.")
    w(f"- Monthly projection uses {args.workdays} working days/month.")
    if d.has_perf:
        w("- Performance metrics use only hours in which a host had at least one "
          "connected session. Required vCPU/user = (peak-hour CPU p95 ÷ "
          f"{CPU_TARGET_PCT:.0f}%) × host vCPU ÷ peak concurrent sessions, averaged "
          f"over hosts. Thresholds are heuristics: oversized < {CPU_OVERSIZED_PCT:.0f}%, "
          f"saturated ≥ {CPU_SATURATED_PCT:.0f}%, input-delay warning ≥ "
          f"{INPUT_DELAY_WARN_MS:.0f} ms p95.")
        w("- Memory/user = (host RAM − minimum available MB in the hour) ÷ concurrent "
          "sessions. It includes OS and shared overhead, so it overstates per-user "
          "need on lightly loaded hosts.")
    if d.has_apps:
        w("- The application profile counts the hours and session-days in which an "
          "image name appeared in the per-process input-delay counter. It shows what "
          "ran with user focus, not CPU consumed per application. Shell and platform "
          "processes are excluded.")
    w("- 'Software-Only / GCP' segmentation from the source methodology is not "
      "applicable: AVD runs only on Azure.")
    w("- Where a VM's vCPU could not be resolved, vCPU/user is shown as n/a.")
    w("")
    return "\n".join(L)


_HTML_CSS = """
body{font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;
 line-height:1.5;color:#1a1a2e;max-width:960px;margin:2rem auto;padding:0 1.2rem}
h1{border-bottom:3px solid #5a3fc0;padding-bottom:.3rem}
h2{margin-top:2rem;border-bottom:1px solid #ddd;padding-bottom:.2rem;color:#3a2a80}
h3{color:#5a3fc0;margin-top:1.4rem}
table{border-collapse:collapse;width:100%;margin:1rem 0;font-size:.92rem}
th,td{border:1px solid #d0d0e0;padding:.4rem .6rem;text-align:left}
th{background:#f0edfa}tr:nth-child(even) td{background:#fafafe}
code{background:#f3f2fa;padding:.1rem .3rem;border-radius:3px;font-size:.9em}
pre{background:#1e1e2e;color:#e8e8f0;padding:1rem;border-radius:6px;overflow-x:auto;
 font-size:.82rem;line-height:1.25}
pre code{background:none;color:inherit}
em{color:#666}hr{border:none;border-top:1px solid #ddd;margin:2rem 0}
"""


def _md_inline(t):
    t = html.escape(t)
    t = re.sub(r"`([^`]+)`", r"<code>\1</code>", t)
    t = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"(?<!\*)\*([^*\s][^*]*)\*(?!\*)", r"<em>\1</em>", t)
    t = re.sub(r"(?<![\w])_([^_]+)_(?![\w])", r"<em>\1</em>", t)
    return t


def md_to_html(md, title="AVD Persona Mapping — Report"):
    """Minimal Markdown -> self-contained HTML for the report we generate."""
    out, i, lines = [], 0, md.split("\n")
    in_code = False
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            if not in_code:
                out.append("<pre><code>"); in_code = True
            else:
                out.append("</code></pre>"); in_code = False
            i += 1
            continue
        if in_code:
            out.append(html.escape(line)); i += 1
            continue
        if line.startswith("### "):
            out.append(f"<h3>{_md_inline(line[4:])}</h3>")
        elif line.startswith("## "):
            out.append(f"<h2>{_md_inline(line[3:])}</h2>")
        elif line.startswith("# "):
            out.append(f"<h1>{_md_inline(line[2:])}</h1>")
        elif line.strip() == "---":
            out.append("<hr>")
        elif line.lstrip().startswith("- "):
            indent = len(line) - len(line.lstrip())
            out.append("<ul>" if indent < 4 else "<ul style='margin-left:1rem'>")
            while i < len(lines) and lines[i].lstrip().startswith("- "):
                out.append(f"<li>{_md_inline(lines[i].lstrip()[2:])}</li>")
                i += 1
            out.append("</ul>")
            continue
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i]); i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            out.append("<table>")
            if cells:
                out.append("<tr>" + "".join(f"<th>{_md_inline(c)}</th>" for c in cells[0]) + "</tr>")
                for r in cells[2:]:  # skip |---| separator
                    out.append("<tr>" + "".join(f"<td>{_md_inline(c)}</td>" for c in r) + "</tr>")
            out.append("</table>")
            continue
        elif line.strip():
            out.append(f"<p>{_md_inline(line)}</p>")
        i += 1
    body = "\n".join(out)
    return (f"<!doctype html><html><head><meta charset='utf-8'>"
            f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>{_HTML_CSS}</style></head>"
            f"<body>{body}</body></html>")


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
            comment = f"pool={g['group']}; region={g['pool'].get('region','')}"
            if g["measured_vcpu_per_user"] is not None:
                comment += f"; measured vCPU/user={g['measured_vcpu_per_user']:.2f}"
            if g["apps"]:
                comment += "; top apps=" + "/".join(a["process"] for a in g["apps"][:5])
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
                comment,
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
    with open(os.path.join(out_dir, "report.html"), "w", encoding="utf-8") as f:
        f.write(md_to_html(report))
    write_template_csv(os.path.join(out_dir, "persona_mapping.csv"), d, groups, usage_group)

    print(report)
    print(f"\n[written] {os.path.join(out_dir, 'report.md')}   (Markdown)")
    print(f"[written] {os.path.join(out_dir, 'report.html')}   (open in a browser)")
    print(f"[written] {os.path.join(out_dir, 'persona_mapping.csv')}   (open in Excel)")


if __name__ == "__main__":
    main()
