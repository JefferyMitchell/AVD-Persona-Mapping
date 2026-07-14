---
layout: home
title: Home
nav_order: 1
permalink: /
---

# AVD Persona Mapping
{: .no_toc }

A two-phase, **read-only** discovery toolkit that lets you safely share your Azure Virtual Desktop (AVD) environment — configuration and real usage — with a Citrix architect, so migration and persona-mapping conversations are grounded in your actual numbers rather than assumptions.

You run everything in your own Azure Cloud Shell. **Nothing leaves your control** until *you* generate a time-limited, read-only link and share it.

---

## How It Works

| Phase | Who runs it | What it does |
|---|---|---|
| **[Phase 1 — Setup](phase-1-setup.md)** | A customer administrator, once | Creates a throwaway resource group, a private storage account, and a **scoped read-only collector identity** |
| **[Phase 2 — Collect](phase-2-collect.md)** | The collector identity | Reads host-pool config + usage, hashes usernames, uploads results to the storage account |
| **[Share](sharing.md)** | The customer | Generates a read-only, 7-day SAS link and shares it with the architect |
| **[Cleanup](cleanup.md)** | The customer | One command removes everything the toolkit created |

## The Security Model

Your security team will scrutinise this, so the design is deliberately split by privilege level:

- The **collector identity is read-only against the entire environment.** It genuinely cannot modify host pools, session hosts, VMs, or anything else.
- Its *only* write capability is confined to a **single throwaway storage account** created for the assessment.
- This is enforced by **role scoping**, not a vague "read-only account" claim — you can point to exactly three role assignments.

A single identity can't both *create* a storage account and be *read-only* — creating and uploading are writes. So resource creation lives in the admin-run setup phase; the collector identity is read-only-plus-one-storage-account.

> **PII:** usernames are SHA-256 hashed with a per-run salt *before they ever hit disk*. Raw usernames are only kept if you explicitly pass `-r`.

## Who This Is For

| Role | What you'll do here |
|---|---|
| Azure / Cloud Administrator | Run Phase 1 to stand up the scoped collector identity |
| Security reviewer | Verify the exact three role assignments before anything runs |
| Citrix Architect | Receive the shared results and build the persona mapping |

## Quick Start

```
1. Prerequisites  →  docs/prerequisites
2. Phase 1: Setup →  docs/phase-1-setup
3. Phase 2: Collect →  docs/phase-2-collect
4. Share the read-only SAS link
5. Clean up when done
```

## Disclaimer

This project is not affiliated with or endorsed by Microsoft or Citrix. It is a community reference implementation. Review the scripts and test in a non-production subscription before running against production.
