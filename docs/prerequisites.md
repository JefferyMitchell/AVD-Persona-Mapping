---
layout: default
title: Prerequisites
nav_order: 2
---

# Prerequisites
{: .no_toc }

<details open markdown="block">
  <summary>Contents</summary>
  {: .text-delta }
- TOC
{:toc}
</details>

---

## Where you run this

Everything runs in **Azure Cloud Shell (Bash)** — open it from the Azure portal (the `>_` icon in the top bar) or at [shell.azure.com](https://shell.azure.com). Cloud Shell already has the `az` CLI and `jq` installed and is already authenticated as you, so there is nothing to install locally.

You can also run it from any machine with the Azure CLI and `jq`, but Cloud Shell is the path of least resistance and keeps everything inside Azure.

## Permissions

The two phases need different privilege levels — this split is the whole point of the security model.

| Phase | Identity | Rights required |
|---|---|---|
| **Phase 1 — Setup** | A customer administrator | Create a resource group + storage account, and assign roles at subscription scope (e.g. **Owner** or **User Access Administrator** + **Contributor**). For the default `sp` model, also the Entra rights to create an app registration. |
| **Phase 2 — Collect** | The scoped collector identity created in Phase 1 | None to arrange — Phase 1 grants exactly what it needs |

> **SP creation is locked down?** Use the custom-role model (`./avd-setup.sh -m role`) instead. It creates a tightly scoped custom role assigned to your own user rather than a service principal. See [Phase 1 — Setup](phase-1-setup.md#identity-models).

## Get the toolkit

Clone the repository inside Cloud Shell:

```bash
git clone https://github.com/JefferyMitchell/AVD-Persona-Mapping.git
cd AVD-Persona-Mapping
chmod +x avd-setup.sh avd-collect.sh
```

## Usage data requires diagnostics (important)

The **configuration** data (host pools, session hosts, app groups) is *always* available.

The **usage** data (concurrency, per-host density, named-vs-active shelfware) comes from Log Analytics and requires that your AVD environment is already sending diagnostics there:

- Azure Monitor **diagnostic settings** on your host pools forwarding AVD logs (notably `WVDConnections`) to a **Log Analytics workspace**
- Workspace **retention** covering the lookback window you intend to use (default 30 days)

> **No diagnostics configured?** The toolkit still runs and collects full configuration data — it simply skips the usage sections. That absence is itself a finding worth discussing: it means there is currently no visibility into how the estate is actually used.

---

Next: [Phase 1 — Setup](phase-1-setup.md)
