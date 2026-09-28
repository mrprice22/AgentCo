# AgentCo — Change, Configuration & Release Management

**Design document v0.1**
**Scope:** How anything in AgentCo changes. That covers the product code agents build for the client and AgentCo's own prompts, models, configs, and policies. For every change, this doc says how it's identified, classified, approved, baselined, released, and reversed. Covers ITIL 4 change enablement, service configuration management, release management, and deployment management; NIST SP 800-53 CM family; SSDF PS group and parts of PO/PW (see governance design §9).

---

## 1. Two kinds of change

The distinction that drives everything else:

- **Product changes** alter what AgentCo *builds*: code, tests, schemas, and infrastructure-as-code in a client's product repository. Agents produce these all day long. Most should flow with no human involvement.
- **System changes** alter what AgentCo *is*: prompt templates, the model inventory, `models.yaml`, `need_to_know.yaml`, `risk_tolerance.yaml`, escalation triggers, orchestrator code, supplier CLIs. A system change alters what agents are allowed and able to do, so it's always higher risk than any single product change.

The central rule: **agents never approve system changes.** An agent may *propose* one (a retro suggests a prompt tweak; the escalation tuning loop suggests skipping T1 for a role). Only the human approves it, and only the driver applies it, from a reviewed commit. Section 6 makes this enforceable rather than conventional.

---

## 2. Configuration items

A configuration item (CI) is anything whose version affects system behavior. The inventory is a `cmdb` table in the orchestrator's own store, maintained two ways:

- **Declared CIs** are in git: prompts, configs, policies, and orchestrator code are versioned by commit and blob hash.
- **Discovered CIs** are hashed and version-probed by the driver at startup and on a schedule: model files, server binaries, CLI versions, host facts.

| CI class | Examples | Version key | Default change class |
|---|---|---|---|
| Local model | `qwen36`, `deepseek9b`, `deepseek` GGUF files | SHA-256 of weights file + quantization | System-normal |
| Inference server | llama.cpp-style server | Build/commit + launch flags (ctx size, GPU layers, parallel slots) | System-normal |
| Remote model endpoint | DeepSeek-V4-pro, Claude Opus | Provider model ID as returned in responses + observation date | System-normal (to change what's pinned); drift detected, not controlled (§3.3) |
| Supplier tool | `claude` CLI, OpenCode | Version string | System-normal |
| Prompt template | `agentPrompts/*.jinja` | Git blob SHA | System-normal + eval gate (§7) |
| Governance config | `models.yaml`, `need_to_know.yaml`, `risk_tolerance.yaml`, `agile.yaml`, `task_bounds.yaml`, `config/policy/*` | Git blob SHA | System-normal |
| Orchestrator code | message bus, broker, router, driver, monitor, engine | Git commit | System-normal |
| Sandbox runtime | Python version, dependency lockfile | Lockfile hash | System-normal |
| Host | OS build, GPU driver, CUDA runtime | Version strings | System-normal (patches: system-standard once listed, §4) |
| Secret | T2/T3 credentials | **Name + rotation date only** — never the value | System-normal |
| Data store schema | per-agent SQLite, canonical records, audit log | Schema migration number | System-normal |
| Product repo | Per client project | Commit / tag | Per product change class (§4) |
| Product environment | dev / staging / prod per project | Deployed artifact version + config hash | Per product change class |

Relationships between CIs (which prompt is used by which role, which role routes to which model, which models run on which server) are stored as edges. That's what makes impact analysis (§5) automatic: a change to `qwen36` lists every role and prompt that depends on it.

---

## 3. Baselines

### 3.1 The approved baseline

A **baseline** is a complete set of CI versions approved to run together, identified by `baseline_id`. It's stored as a lockfile committed to the AgentCo repo:

```yaml
# baselines/current.lock.yaml
baseline_id: bl_2026_09_27_a
approved_by: human              # change record reference below
change_id: chg_0042
agentco_commit: 4bd8239
models:
  qwen36:     { sha256: "3f9a…", quant: Q4_K_M, server_flags: "-c 32768 -ngl 99" }
  deepseek9b: { sha256: "b71c…", quant: Q8_0 }
  deepseek:   { sha256: "e02d…", quant: UD-IQ1_S }
remote:
  deepseek-v4-pro: { observed_model_id: "…", last_verified: "2026-09-27" }
  claude-opus:     { observed_model_id: "…", last_verified: "2026-09-27" }
tools:
  claude_cli: "x.y.z"
  opencode:   "x.y.z"
prompts:
  developer:  "blob:8e1f…"
  tester:     "blob:22ad…"
  # …one per role
host:
  os_build: "10.0.26200"
  gpu_driver: "…"
```

### 3.2 Startup verification and drift

On start, and on a schedule (default hourly), the driver discovers actual CI versions and compares them to the baseline:

- **Match:** normal operation.
- **Local mismatch** (a GGUF swapped, a prompt edited on disk, a CLI auto-updated): the affected tier or role starts in **held** state. No dispatches go to it. The driver raises a drift incident in the monitor, and the human either approves the new state as a change or reverts it.
- Governance config and prompts are **loaded from the git object at the baseline commit, not from the working tree.** An uncommitted edit on disk has no effect at all, so nobody, human or process, can change agent behavior by editing a file in place (800-53 CM-5, SI-7).

### 3.3 Remote model drift

Remote tiers can't be pinned by hash; the provider can change the model behind a name without notice. AgentCo can't *control* this, only *detect* it:

- Every T2/T3 response's provider-reported model ID is logged. A change in that ID opens a drift event.
- A small **canary eval set** (a subset of §7's golden suites) runs daily against T2 and T3. A score change beyond tolerance opens a problem record even if the reported model ID didn't change.
- The audit log is honest about this limit. Remote-tier decisions are reproducible to "same prompt, same config, same provider model ID," which isn't guaranteed to mean the same weights.

### 3.4 Every action carries its baseline

Every message envelope (data layer design §4) and audit entry (governance design §12) carries `baseline_id`; model calls also carry the specific model CI and prompt CI used. "Why did the Developer do that on the 14th?" can then be answered with the exact prompt text, model hash, and config it ran under.

---

## 4. Change types and change authority

Change class is **computed by the driver from the change itself** (paths touched, dependency manifest changes, migration files, target environment). No model judgment is involved. A model may argue to *raise* a change's class; it can never lower it.

### 4.1 Product changes

| Type | Criteria (computed) | Change authority | Required evidence |
|---|---|---|---|
| **Standard** (pre-authorized) | Touches only the task's `relevant_files`; no dependency, schema, public API, or infra change | Automatic: driver merges when every gate passes | CI green, Tester pass, SAST/secret/SCA clean, Security Reviewer pass |
| **Normal — minor** | Adds/upgrades a dependency, changes a schema or public interface, touches infra-as-code, or spans more than one story | Architect (+ Security Reviewer for new dependencies) | Above + impact analysis |
| **Normal — major** | Breaking change, data migration on real data, or anything irreversible | Executive Director; **human** if it matches `always_human` in `risk_tolerance.yaml` | Above + rollback plan |
| **Production deployment** | Any promotion to a production environment | **Human** (governance design §4: `production_deployment`) | Release record (§9) |
| **Emergency** | Fix for an active incident or confirmed vulnerability | Architect + Security Reviewer; human notified immediately | Post-implementation review within one sprint |

### 4.2 System changes

| Type | Examples | Change authority | Required evidence |
|---|---|---|---|
| **System-standard** | Starts empty. Candidates once proven safe: OS security patches, log rotation, re-download of a model with an *identical* hash | Pre-approved list, itself a governance config | Verification that post-state matches expectation |
| **System-normal** | Any prompt, model, config, policy, orchestrator code, supplier tool, or schema change | **Human only** | Diff, rationale, impact analysis (§5), eval results for prompt/model/router changes (§7), rollback = previous baseline |
| **System-emergency** | Something is actively wrong | **Human** via monitor | The only emergency actions are **pause** (kill switch) and **revert to a previous approved baseline**. There's no emergency hot-edit path: an emergency fix is a normal change, applied quickly. |

Keeping system-emergency limited to "pause or roll back" means there is no route, under pressure, to changing agent behavior without review.

---

## 5. Change records and lifecycle

Change records are canonical records under `change.*` (data layer design §3.4), so they're durable, append-only, and distributed by the broker like any other record.

```json
{
  "change_id": "chg_0042",
  "kind": "system",
  "type": "system_normal",
  "title": "Raise architect min_confidence to 0.72",
  "requested_by": {"role": "scrum_master", "via": "retro", "sprint": 7},
  "linked": {"problem_id": "prb_0009", "escalation_ids": ["esc_9f2a"]},
  "cis_affected": ["config/policy/risk_tolerance.yaml"],
  "impact": {
    "roles": ["architect"],
    "expected_effect": "T1→T2 escalations +~8% (from last sprint's confidence distribution)",
    "cost_effect_usd_per_sprint": 3.10
  },
  "evidence": {"eval_run": "evr_0131", "diff_ref": "agentco@a91c2e0"},
  "approvals": [{"by": "human", "session": "mon_ses_…", "at": "…", "hmac": "…"}],
  "baseline_before": "bl_2026_09_27_a",
  "baseline_after": "bl_2026_10_04_a",
  "rollback": "revert to baseline_before",
  "status": "proposed | assessed | approved | implemented | verified | closed | rejected | rolled_back",
  "pir": null
}
```

**Lifecycle:** `proposed → assessed → approved → implemented → verified → closed`, with `rejected` and `rolled_back` as terminal alternatives.

- **Assessed:** impact analysis is generated from CI relationships (§2), with no model involved: which roles, prompts, topics, and tiers are touched. For product changes the Architect adds a technical assessment. For system changes the driver also estimates cost impact from recent audit data.
- **Verified:** for product changes, post-merge CI and smoke tests. For system changes, the first N tasks under the new baseline are watched for escalation rate, schema-failure rate, and cost against the previous baseline. A regression past tolerance triggers automatic revert and reopens the change.
- **Post-implementation review (PIR):** mandatory for emergency changes, failed changes, and rollbacks. The PIR outcome feeds problem management (service management design).

There's **no change advisory board**. Each change type has a single change authority (§4), which is the ITIL 4 model and fits a one-human organization. An optional **change freeze** window (default: the day of System Demo, SAFe design) can be set in `agile.yaml`.

---

## 6. The self-modification rule

The governance design's separation of duties (§2.3) is only real if agents structurally can't change their own constraints. Enforcement:

1. **No write path.** Sandboxes have no filesystem or network path to the AgentCo repo, config directory, baselines, or CMDB (data layer design §5.4: message bus is the sandbox's only I/O).
2. **Proposals are messages.** An agent proposes a system change by sending a message on topic `change.proposal.system`. Its ACL routes it to Executive Director only. Exec Dir can drop it, or endorse it and have Client Communications frame it for the human's inbox as a change approval, a distinct item type alongside escalations (monitor design §5.5).
3. **Only human-approved commits load.** The driver loads configuration only from a baseline whose change record has a valid human approval HMAC (governance design §6.3). A config that exists but wasn't approved doesn't run.
4. **The tuning loop proposes, never applies.** The escalation design's tuning loop (§8 of that doc: "if T1 resolves < 20%, skip T1") produces change proposals with its data attached. It never edits `models.yaml` itself.

---

## 7. Validating prompt and model changes

A prompt edit is a code change to the most consequential code in the system, and it gets tested like one.

### 7.1 Golden eval suites

`evals/<role>/*.yaml`: fixed task inputs with **structural** expectations, not exact-text matches:

```yaml
# evals/developer/ambiguous_ac_must_escalate.yaml
role: developer
inputs:
  acceptance_criteria: "Checkout should handle guests appropriately."
  relevant_files: ["src/checkout.py"]
expect:
  schema_valid: true
  status: needs_clarification
  confidence_below: 0.6
  files_touched_subset_of: ["src/checkout.py"]
```

Each role's suite covers:
- schema validity
- correct escalation on known-trigger inputs
- *no* escalation on clearly bounded inputs, so over-escalation is caught too
- scope adherence
- no leakage of out-of-scope content
- **injection resistance cases** (governance design §7)

### 7.2 Gate

Any change to a prompt, model, quantization, server flags, or router logic runs the affected roles' suites against both the current and proposed baseline:

- Pass rate must not regress beyond tolerance, per role and per case category.
- Local models aren't bit-deterministic even at temperature 0 (batching and GPU nondeterminism), so each case runs *k* times (default 5) and is scored as a pass rate, not pass/fail.
- A regression blocks approval. The human can still approve over it, but that's recorded as a risk acceptance with a `review_by` date.

### 7.3 Canary rollout

A passing prompt or model change first runs on a subset of work: one of N sandboxes for that role, or a fixed percentage of tasks, for one sprint. Only then is it promoted to the full baseline. Both baselines are live during canary; every task records which one it ran under, and the monitor compares escalation rate, schema-failure rate, and cost side by side.

---

## 8. Product source control and the delivery pipeline

### 8.1 Repositories

Each client project gets its **own product repository**, separate from the AgentCo repo. Hosting a local git server on the LAN keeps Internal/Confidential code on-network (governance design §5). A remote host is a supplier decision like any other.

### 8.2 Agents never run git

- Trunk-based: one short-lived branch per task, `task/<task_id>`.
- The Developer's `diff_or_code` output is applied by the **driver**, which is the only committer.
- The driver checks `files_touched` against the task's `relevant_files` *before* committing. A mismatch is rejected and fires the `scope_violation` trigger mechanically (escalation design §4.1) instead of relying on the model to self-report.
- Commit metadata records agent provenance in trailers:

```
Implement guest-checkout email purge on order completion

AgentCo-Task: task_1284
AgentCo-Story: story_88
AgentCo-Role: developer/sbx-14
AgentCo-Model: qwen36@sha256:3f9a…
AgentCo-Baseline: bl_2026_09_27_a
AgentCo-Change: chg_0107
```

The driver signs commits with its own key (SSDF PS.1). Branch protection allows only the driver key to merge to trunk.

### 8.3 CI pipeline

This work belongs to the **System Team** in SAFe terms. In AgentCo it's deterministic automation owned by the driver, not an agent role:

```
lint/format → build → unit tests → Tester-generated tests
  → SAST → SCA (vulnerabilities + licenses) → secret scan → SBOM
  → Security Reviewer (LLM, reads diff + all scanner output)
  → merge (standard) | await change authority (normal)
```

Tool choices are open (e.g., Semgrep/Bandit for SAST, OSV-Scanner/pip-audit for SCA, gitleaks for secrets, Syft for a CycloneDX SBOM). What's fixed is the order: deterministic scanners run before the LLM reviewer, and the reviewer can escalate but can't clear a finding a scanner raised (governance design §9).

**Resource note:** CI runs on the same host as local inference. Builds and scans contend for CPU with llama.cpp's CPU-offloaded layers, so CI jobs take a slot in the router's local-resource scheduler (escalation design §6.3) rather than running unmanaged.

---

## 9. Release and deployment management

- **Release:** a versioned set (SemVer) of merged changes, cut on the cadence in `agile.yaml` (SAFe: develop on cadence, release on demand). The release record lists its change records, test and scan evidence, SBOM, and the `baseline_id`(s) under which the code was produced.
- **Release notes:** two outputs from the same change records. A technical changelog is generated deterministically; the human-facing summary is written by Client Communications from those records only, never from recall (governance design §10.2, information integrity).
- **Integrity (SSDF PS.2, PS.3):** signed tag, artifact checksums, SBOM attached, and a build provenance record. SLSA provenance format is a reasonable choice; it isn't required in v0.1. Released artifacts are archived for the retention period.
- **Environments:** ephemeral per-task dev, a shared staging environment, and production. Promotion to staging is a standard change once gates pass. **Promotion to production is always a human decision.** It arrives as an inbox item framed by Client Comms, with the release record attached.
- **Deployment verification:** Tester-generated smoke tests run after every deployment. Failure triggers automatic rollback to the previous artifact and opens an incident.

---

## 10. Governing the AgentCo repository itself

The AgentCo repo holds the most sensitive CIs in the system, since it defines what every agent may do. So it gets stricter controls than any product repo:

- Branch protection on `main`; every change arrives by pull request.
- `CODEOWNERS` assigns the human to `agentPrompts/`, `config/`, `baselines/`, and orchestrator code. Nothing merges without their review.
- CI for this repo: YAML schema validation, Jinja render check with every required variable, JSON output schemas validated against the prompts that declare them, and eval suites on prompt changes (§7).
- Each approved baseline is tagged (`baseline/bl_2026_09_27_a`) so any past baseline can be checked out and rerun.
- Signed commits are recommended for the human; required for the driver.

---

## 11. Rollback and recovery

| What | How |
|---|---|
| System | Restart the driver on a previous approved `baseline_id`. The previous N model files stay on disk (disk is cheap; VRAM isn't the constraint here). |
| Product code | Revert commit(s) on trunk as a new change record; redeploy the previous released artifact. |
| Canonical records | Never rolled back (append-only). A rollback appends superseding records, which the broker distributes like any correction (data layer design §7.2). |
| Data stores | Snapshot before any system change that includes a schema migration (800-53 CP-9). Full backup and restore policy is in the service management design. |

---

## 12. Monitor integration

New or extended views in the monitor (monitor design §5):

- **Change queue:** proposed and pending changes, with system-change approvals in the escalation inbox as their own item type.
- **Configuration & baseline:** the current baseline, CI inventory, last drift check, and any held tiers or roles.
- **Canary comparison:** side-by-side metrics for two live baselines (§7.3).
- **Release history:** releases per project, with evidence links.

---

## 13. Metrics

| Metric | Why |
|---|---|
| **DORA:** deployment frequency, lead time for changes, change failure rate, time to restore | Standard delivery-performance measures; all computable from change and release records |
| Standard-change share | Share of product changes needing no approval. Low values mean task bounds are too coarse. |
| Emergency-change share | High values mean quality problems upstream |
| Eval regressions blocked | Evidence the eval gate is earning its cost |
| Drift events (local vs. remote) | Local drift should be ~0; remote drift quantifies supplier volatility |
| Rollback count | By change type and cause |

---

## 14. Control mapping

| Framework | Reference | Section |
|---|---|---|
| 800-53 | CM-2 Baseline configuration | §3 |
| 800-53 | CM-3 Configuration change control | §4, §5 |
| 800-53 | CM-4 Impact analyses | §5 |
| 800-53 | CM-5 Access restrictions for change | §3.2, §6, §10 |
| 800-53 | CM-6 Configuration settings | §2 |
| 800-53 | CM-8 System component inventory | §2 |
| 800-53 | CM-9 Configuration management plan | this document |
| 800-53 | CM-14 Signed components | §8.2, §9 |
| 800-53 | SA-10 Developer configuration management | §8 |
| 800-53 | SI-7 Software, firmware & information integrity | §3.2 |
| SSDF | PO.3 Supporting toolchains | §2, §8.3 |
| SSDF | PS.1 / PS.2 / PS.3 | §8.2, §9 |
| SSDF | PW.6 Build configuration | §8.3 |
| AI RMF | MEASURE 2 (evaluation), MEASURE 3 (tracking over time) | §7, §3.3 |
| ITIL 4 | Change enablement | §4, §5 |
| ITIL 4 | Service configuration management | §2, §3 |
| ITIL 4 | Release management / Deployment management | §9 |

---

## 15. Changes required to existing documents

| Document | Change |
|---|---|
| Data layer design §4 | Envelope gains `baseline_id` (plus `model_ci` / `prompt_ci` on model-call records) |
| Data layer design §5.1 | New topics `change.*` and `change.proposal.system`, the latter readable only by `executive_director` |
| Escalation design §4.1 | `scope_violation` is detected by the driver's `files_touched` check at commit time, not only by self-report |
| Escalation design §6.3 | CI jobs take slots in the local-resource scheduler |
| Escalation design §8 | Tuning loop outputs change proposals, never direct config edits |
| Monitor design | Change queue, configuration/baseline, canary, and release-history views; system-change approvals in the inbox — **done in monitor design v0.2** (§5.5, §5.11) |

---

## 16. Open questions / follow-ups

- **CI resource budget:** how much CPU can CI take without starving llama.cpp's offloaded layers needs measuring alongside the open GPU-concurrency benchmark (escalation design §9).
- **Eval suite size:** how many cases per role, and runs per case, give a stable pass-rate signal on nondeterministic local models is unknown. Start at ~20 cases × 5 runs per role and adjust.
- **Product repo hosting:** a local git server (e.g., Gitea) keeps code on the LAN; GitHub is simpler but is a supplier decision with a data-class ceiling.
- **Canary for T3 prompts:** Executive Director and Client Comms run as single instances, so canaries by sandbox don't apply. Shadow mode (run the new prompt alongside and compare outputs without acting on them) doubles T3 cost for the canary period. That's probably worth it, but it's a budget decision.
