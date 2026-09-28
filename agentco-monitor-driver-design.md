# AgentCo — Monitor/Driver Design

**Design document v0.2**
**Scope:** The one component in this system that is *not* an agent: the process the human actually runs, watches, and occasionally talks back to. Covers:
- how it hosts the core engine and supervises agent sandboxes
- how it controls configuration, commits, and releases
- how it alerts the human when things go wrong
- how it surfaces backlog, iteration, roadmap, cost, service, change, and compliance state
- how every human decision (escalations, change approvals, production releases, risk acceptances) reaches the human and gets relayed back

| Version | Changes |
|---|---|
| v0.1 | Original design: driver/monitor split, views, human decision flow, roadmap edits, GPU contention view, LAN security notes |
| v0.2 | Integrates the core engine, governance, change/config, service management, and company directive designs. The driver now hosts the engine, owns baselines, commits, CI, alerts, and backups. The monitor gains service, change/config, incident, and compliance views, an expanded inbox, trace/replay, and authenticated writes. Resolves v0.1's open questions on notifications, auth, and historical replay. |

---

## 1. What this program is and isn't

It's one program with two logical halves, which matters enough to name separately even though they ship together:

- **Driver:** the control plane. Its jobs:
  - hosts the core engine (core engine design §1)
  - supervises agent sandbox processes
  - runs the clocks behind ceremonies and deadlines
  - is the entry point for the human's input
  - loads and verifies the approved configuration baseline
  - is the only committer to product repositories
  - runs CI and deployments
  - sends operational alerts
  - delivers anything Claude Opus decides needs the human (§6)
- **Monitor:** the observation plane, and the human's only write surface. It provides views over work, forecasts, cost, service levels, changes, incidents, and compliance, plus a small set of authenticated actions, each of which becomes a command to the driver (§7).

**It is deliberately not an agent.** It has no role in the org chart, no tier, no model backend, and no escalation ladder. This matters for the compartmentalization design in particular: the whole point of need-to-know enforcement (data layer design §5) is to stop *agents* from building up a full picture they haven't earned. The human already sits above that hierarchy by construction, as the client the whole company serves. So the monitor is explicitly exempt from the broker's ACL and reads everything. That privilege must never leak to an agent. The monitor can't be prompted, can't answer questions, and has no LLM anywhere in its execution path. It's plumbing, not a participant.

The same rule covers everything the driver hosts. The engine is deterministic, with no LLM inside (core engine design §1), and so are the alert generator and the daily digest (§4.8). Agents are invoked *by* the driver; nothing in the driver *is* one.

**Two channels to the human.** The escalation design says only Opus reaches the human, and that holds for **conversation**: anything an agent wants the human to know or decide goes through `human_relay` → Client Communications → the monitor inbox. The driver also sends **operational alerts**: templated, factual notices about the *state of the system*, with no agent-authored content (service management design §1.3). The alert channel exists because the conversation channel can itself fail. If T3 is down, only the driver can tell the human so.

---

## 2. Architecture overview

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                                   DRIVER                                      │
│                                                                                │
│   ┌──────────────────────────────────────────────────────────────────────┐   │
│   │                 ENGINE CORE  (core engine design)                      │   │
│   │   org · work model · workflow FSMs · scheduler · effects · ledger      │   │
│   │   in: events ─────────────────────────────────────────▶ out: commands  │   │
│   └───────────────────────────────▲──────────────────────────┬───────────┘   │
│                                   │ events                    │ commands      │
│  ┌────────────────┐ ┌─────────────┴──┐ ┌────────────────┐ ┌──▼─────────────┐ │
│  │process_supervisor│ │ clock adapter  │ │ intake_gateway │ │ repo + CI      │ │
│  │sandboxes, pause, │ │ iteration/PI,  │ │ human input →  │ │ sole committer,│ │
│  │held state        │ │ deadlines,     │ │ Client Comms   │ │ scope check,   │ │
│  └────────────────┘ │ ceremonies     │ └────────────────┘ │ CI, deploy     │ │
│  ┌────────────────┐ └────────────────┘ ┌────────────────┐ └────────────────┘ │
│  │baseline loader │ ┌────────────────┐ │ human_relay    │ ┌────────────────┐ │
│  │+ drift detector│ │ model router   │ │ adapter (T3    │ │ alerts + digest│ │
│  │(loads approved │ │ (escalation    │ │ only) → inbox  │ │ + notification │ │
│  │ commit only)   │ │ design; CLIs,  │ └────────────────┘ │ adapter        │ │
│  └────────────────┘ │ breakers)      │ ┌────────────────┐ └────────────────┘ │
│                     └────────────────┘ │ backup jobs    │                    │
│                                        └────────────────┘                    │
└──────────────────────────────┬───────────────────────────────────────────────┘
                               │ message_bus + broker (data layer design)
                               │ read-only tap (bypasses broker ACL)
┌──────────────────────────────┼───────────────────────────────────────────────┐
│                         MONITOR                                                │
│  ┌──────────────┐   ┌────────▼───────┐   ┌─────────────────────────────────┐ │
│  │ event_tap     │──▶│ backend (API)   │──▶│ web UI (localhost by default)    │ │
│  │ bus + engine  │   │ REST + WS;      │   │ boards · inbox · traces · cost · │ │
│  │ events        │   │ authenticated   │   │ service · change · compliance    │ │
│  └──────────────┘   │ writes → driver │   └─────────────────────────────────┘ │
│                      │ commands        │                                        │
│                      └────────┬───────┘                                        │
│                               │ reads (never writes)                           │
│    ┌──────────────────────────▼─────────────────────────────────────────────┐ │
│    │ engine event log + projections · canonical_records · audit log · CMDB    │ │
│    └──────────────────────────────────────────────────────────────────────────┘ │
└────────────────────────────────────────────────────────────────────────────────┘

   external watchdog (Windows scheduled task): checks driver + monitor heartbeats → alert
```

**The monitor never writes to a store.** In v0.1 the monitor wrote the human's answers directly into `canonical_records`. In v0.2 every monitor action becomes a **command** to the driver (§7). The engine validates the command and applies its effects, like any other proposal, so the engine's event log stays the single writer for work state (core engine design §11). The monitor also never reads agent-private memory (`episodic_log` / `semantic_notes`). Those stay agent-private per the data layer design, and nothing a human needs to track lives there.

---

## 3. Data sources

| View needs | Reads from |
|---|---|
| Backlog / iteration boards | Engine **projections** over the event log (core engine design §11): epics, features, stories, tasks and their states. Live deltas via event tap. |
| PI & roadmap | Engine projections + `canonical_records` (`roadmap.*`, `decision.release.*`) + simulation-mode forecasts (P50/P85) |
| Inbox | Engine `human_request` items (escalations, change approvals, deployment approvals, risk acceptance renewals) |
| Decision log (resolved without you) | Audit log entries where `resolution_tier` is T3 and no human was asked |
| Live feed & traces | Event tap (bus + engine events), grouped by `correlation_id` |
| Historical replay | Engine event log + snapshots (core engine design §11.3) |
| Cost & capacity | Cost ledger (core engine design §13); router queue depth, breaker states, model swap counts; capacity plan |
| Service levels | SLIs computed from event log and audit log (service management design §2) |
| Incidents & problems | Engine `incident` / problem records |
| Change & configuration | `change.*` records; CMDB; current and previous baselines; drift status; canary comparisons; release records |
| Compliance | Audit log entries tagged with controls (governance design §12); chain verification status; risk acceptances; model inventory |
| Agent health | `process_supervisor` liveness table |

---

## 4. Driver responsibilities

### 4.1 Hosting the engine

The driver is the host process for the engine core. It provides the real adapters behind the engine's ports: event store, message bus, model router, sandbox pool, repo, CI, and clock (core engine design §2). It restarts the engine from its last snapshot plus the event-log tail after any crash (core engine design §14). The driver itself contains no business logic. What a story is, when a task is ready, and what a failed test means are all engine decisions.

### 4.2 Process supervision

`driver/process_supervisor.py` owns the lifecycle of every agent sandbox process:

- **Starts sandboxes** per the org chart's `instances` range for each role (core engine design §3.1). How many run at once is decided by the engine's scheduler against capacity (core engine design §7), not by the supervisor. This resolves v0.1's open question on multi-sandbox scheduling.
- **Assigns identity:** binds each sandbox's channel to a fixed `sandbox_id` / role / instance / tier, which the bus stamps onto every message (governance design §6.1).
- **Health checks** via heartbeat. A crashed sandbox is restarted; its leased job expires and re-queues (core engine design §14). Task state lives in the event log, not the sandbox, so a restart loses no work.
- **Exposes state** per agent (`running | idle | restarting | crashed | held`). **Held** means baseline drift was detected for that role's prompt or model, so no dispatches go to it until the human resolves the drift (change/config design §3.2).
- **Owns the kill switch:** "pause all," "pause role X," "pause tier X," and "resume." It's reachable only from an authenticated monitor session, never from an agent (no agent has a tool that touches the supervisor). Pause is deliberately low-friction; resume requires re-authentication (§10.2).

### 4.3 Clock and ceremony scheduling

`driver/ceremony_scheduler.py` is now the engine's **clock adapter** (core engine design §10). It emits timer events and the engine decides what they mean:

- **Two cadences** from `agile.yaml`: iterations (agent cadence, default one day) trigger planning, standup-equivalents, and retros. Program Increments (human cadence, default one week) trigger PI review, System Demo, Inspect & Adapt, and the **governance review** (governance design §13).
- **Human request deadlines:** reminder at 50% of the response window, expiry at the deadline, reassessment at 3× the window (service management design §3.3).
- **Risk acceptance `review_by` dates** coming due, raised as inbox renewals (governance design §4).
- **Operational schedules:** hourly drift checks, nightly audit-chain verification, backups (§4.9), per-PI restore drills, daily supplier canary evals, post-iteration calibration of simulation behavior models.

In simulation mode the same adapter is a virtual clock that jumps straight to the next scheduled event (core engine design §12).

### 4.4 Intake gateway

`driver/intake_gateway.py` is where the human's original idea enters the system. Later, it's also where any human-initiated input arrives: a new requirement, a reprioritization, a question about status. It always enters as a message to **Client Communications**, never as a direct write to any agent's memory or to any store. That keeps the property that human input always flows through one door, even for things the UI makes feel like a direct edit (§7).

- Every input gets a **templated acknowledgment within one minute** (service management design §2.3). The driver generates it, so it arrives even when T3 is down.
- In `t3_down` or `remote_down` mode, input is acknowledged, queued, and processed on recovery. The acknowledgment says so.

### 4.5 CLI-backed tier interface

T2 (DeepSeek-V4-pro via OpenCode) and T3 (Claude Opus via the `claude` CLI) are invoked as subprocesses from the router (escalation design §6). The driver:

- Manages subprocess lifecycle (spawn, timeout, capture stdout/stderr, clean shutdown).
- Parses CLI output into the same structured envelope every other tier uses.
- **Holds the only copy of T2/T3 credentials** (Windows Credential Manager or an ignored `.env`). They never enter a sandbox, a prompt, or a log (governance design §6.4).
- Captures token/cost accounting into the cost ledger, estimating from prompt and response length where a CLI doesn't report counts.
- Records the **provider-reported model ID** of every response, for remote drift detection (change/config design §3.3).
- Runs the **egress secret scan** on every outbound T2/T3 prompt before it leaves the host (governance design §5.3).
- Maintains a **circuit breaker** per supplier (service management design §4.4); breaker state drives the engine's service mode.

### 4.6 Configuration and baseline management

The driver is what makes the change/config design enforceable:

- **Loads config and prompts from the approved baseline commit, never from the working tree** (change/config design §3.2). A file edited on disk has no effect. The directive partial (`_company_directive.jinja`) and every role prompt are rendered from the baseline's blobs.
- **Only loads a baseline whose change record carries a valid human approval HMAC** (change/config design §6). An unapproved config never runs.
- **Drift detection** at startup and hourly: models are hashed, and server, CLI, and host versions are probed and compared to the baseline. Local drift puts the affected tier or role in **held** and opens an incident.
- **Applies approved system changes** by restarting onto the new baseline, and supports **human-initiated revert** to any previous approved baseline from the monitor (change/config design §11).
- **Runs canaries:** two baselines live side by side for a trial period, with every task tagged by the baseline it ran under (change/config design §7.3).

### 4.7 Repository, CI and deployment

- **Sole committer.** Agents never run git. The driver applies Developer diffs to `task/<task_id>` branches, **after** checking `files_touched` against the task's `relevant_files`; a mismatch fires `scope_violation` (change/config design §8.2). Commits carry AgentCo provenance trailers and are signed with the driver's key.
- **CI orchestration.** Runs the pipeline (lint → build → tests → SAST → SCA → secret scan → SBOM → Security Reviewer) as deterministic automation, in SAFe terms the System Team function. CI jobs take slots in the local-resource scheduler so builds don't starve llama.cpp's CPU-offloaded layers (change/config design §8.3).
- **Merge** per the change class the engine computes: standard changes merge automatically when all gates pass; normal changes wait for their change authority (change/config design §4.1).
- **Deployment.** Promotion to staging is automatic after gates. Promotion to production happens only after the human approves it in the inbox. A failed smoke test triggers automatic rollback and an incident (change/config design §9).

### 4.8 Operational alerts, notifications and the daily digest

- **Alert generator:** deterministic templates for service-mode changes, Sev1/Sev2 incidents, stalled delivery, and P1 human requests (service management design §2.1, §5.2). It deduplicates on (signal, scope), groups by correlation ID, and sends at most three non-Sev1 alerts a day; the rest roll into the digest (service management design §6.3).
- **Notification adapter:** pluggable channels (desktop toast, email, self-hosted push). At least one channel must reach the human away from the PC. Quiet hours and business hours come from `service_levels.yaml`; only Sev1 and delivery-stopped alerts break quiet hours. Alert content is limited to counts and states, never task content. This resolves v0.1's open question on notification delivery.
- **Daily digest:** generated by the driver from projections, with no LLM. It covers progress, pending requests with accumulated delay, spend against guardrails, service mode, at-risk SLOs, and the **audit chain head hash**, which gives the human's inbox an external tamper-evidence anchor (governance design §12). The PI report is the Client Comms-authored counterpart (service management design §11).

### 4.9 Backup and recovery jobs

The driver runs backups on the schedule in service management design §8: hourly online SQLite backups of the event log, audit log, and canonical records (never raw file copies), nightly per-agent stores, and a verified second copy of model files. It also runs the per-PI **restore drill** into a scratch directory. A failed drill opens a Sev2 incident. Offsite copies are encrypted with a key the human holds, not the driver.

---

## 5. Monitor views

### 5.1 Service mode banner (every page)

The engine's current `service_mode` (`normal`, `local_down`, `t2_down`, `t3_down`, `remote_down`, `budget_exhausted`, `paused`), with what's affected and what the system already did about it (service management design §4.2). When the monitor itself was down, the banner shows the gap after recovery.

### 5.2 Backlog board (Kanban)

Columns by state (core engine design §5.1) across the **Epic → Feature → Story → Task** hierarchy, with business and enabler items visually distinct. Clicking a card shows:
- acceptance criteria, NFRs, WSJF inputs and score
- current owner (role, tier, model)
- **escalation and referral history:** a card that bounced T0→T1→T2, or was referred to the PO twice, looks visibly different from one that sailed through
- a link to its **trace** (§5.7)
- any `depends_on_provisional` marker (service management design §3.4)

### 5.3 Iteration board

Replaces v0.1's sprint board. It shows the same data scoped to the current **iteration** (the agent cadence, core engine design §10), with burndown, WIP limits per state, and blocked items grouped by cause (referral, human request, budget, held role).

### 5.4 PI, roadmap and forecast

A timeline of features and epics across Program Increments. It's rendered from what the Product Owner and Executive Director have published to `roadmap.*`, and the monitor doesn't compute the roadmap itself.
- Each feature shows its **P50/P85 forecast** from simulation mode and a deterministic **RAG** status: green if P85 ≤ target; amber if P50 ≤ target < P85; red if P50 > target (service management design §11.2). Forecasts are always labeled as simulated.
- **What-if panel:** the human can run simulation scenarios, such as dropping a feature, raising a budget, or adding a Developer instance, and see the forecast change. Simulation is deterministic and needs no LLM, so this stays inside the monitor's no-agent boundary. Scenarios never change state; acting on one goes through §7 like any other edit.

### 5.5 Inbox

The one place the human is asked to act. It holds four item types, all rendered with the same structure:

| Type | Source |
|---|---|
| **Escalation decision** | Exec Dir → Client Comms framing (§6) |
| **System change approval** | Change proposal endorsed by Exec Dir (change/config design §6) |
| **Production deployment approval** | Release ready for production (change/config design §9) |
| **Risk acceptance renewal** | `review_by` date reached (governance design §4) |

Each item shows:
- the question and options, plus a recommended option with rationale
- **urgency** (P1–P3, computed by the engine), deadline, and **accumulated delay**
- **impact of waiting**, from simulation: items blocked, share of ready work, forecast delay per day
- the default action on expiry (service management design §3.2)

Stale items float to the top. **Provisional decisions** (if the human has enabled them) stay pinned until confirmed or overturned. The inbox has a **WIP limit** (core engine design §7.1). The count is shown, so the human can see when the system is holding back further questions.

### 5.6 Decision log (resolved without you)

Deliberately placed next to the inbox, not buried: every decision Opus made *without* escalating, with its rationale and source chain. Records whose chain includes human-intake text or reviewed third-party code are flagged for spot-check (governance design §7). This lets the human check whether trusting Opus with a decision was right, and recalibrate `risk_tolerance.yaml` if Opus escalates too much or decides things it shouldn't. Governance design §13 sets a review of this log every iteration.

### 5.7 Live activity feed, traces and replay

- **Live feed:** a scrolling, filterable view of every bus message and engine event, filterable by role, task, topic, or tier. It's display-only and never re-injected.
- **Trace view:** everything sharing a `correlation_id`, from intake through jobs, escalations, referrals, commits, gates, and merge, shown as one timeline (core engine design §11.1). This is the "watch it evolve" view, now linking each outcome to its causes.
- **Replay:** scrub back to any point and see the boards as they were, rebuilt from the event log without calling any model (core engine design §11.3). This resolves v0.1's historical-replay question; retros and incident reviews use it directly.

### 5.8 Cost and capacity

Merges v0.1's cost dashboard (§5.7) and GPU contention section (§8):
- **Cost ledger:** calls, tokens, GPU-seconds, and dollars by tier, per iteration and PI; spend against daily, iteration, and per-epic **budget guardrails** (core engine design §13); unit economics (cost per story point, per feature, per escalation path, cost of `data_class_ceiling` skips).
- **Capacity:** local inference queue depth and wait p90, which sandboxes are blocked on a local slot, **model swap count** (service management design §7.3), `local_queue_overflow` firings, CI slot usage, circuit breaker state per supplier, and the next-PI capacity plan naming the binding constraint.

Together these are the practical instruments for tuning `local_queue_overflow` and the GPU semaphore, whose correct values are otherwise invisible.

### 5.9 Service levels

SLA, OLA, and SLO attainment with error budgets remaining per PI, burn-rate alerts, and any error-budget freeze in effect (service management design §2).

### 5.10 Incidents and problems

Open and recent incidents by severity with timelines (built from traces), automatic first-response actions taken, PIR status, problem records with linked incidents and escalations, the known-error database, and the improvement register (service management design §5, §13, §14).

### 5.11 Change and configuration

- **Change queue:** proposed, pending, and recent changes with type, change authority, evidence, and status (change/config design §5).
- **Baseline & CMDB:** the running `baseline_id`, every configuration item's version and hash, including the company directive version, last drift check, and any held tiers or roles.
- **Canary comparison:** two live baselines side by side on escalation rate, schema-failure rate, and cost.
- **Release history:** per product, with evidence links (SBOM, scans, signed tags).

### 5.12 Compliance

- SSDF gate pass/fail by practice ID and open Security Reviewer findings
- vulnerability remediation SLA status (service management design §10)
- AI RMF evidence by function; 800-53 control coverage from audit-entry control tags (governance design §11, §12)
- audit chain verification status
- risk acceptances with `review_by` dates
- the model inventory
- company-directive principle violation metrics (company directive §4)

This is the artifact a human would actually hand to an auditor.

### 5.13 Agent health

Which sandboxes are running, idle, restarting, crashed, or **held**; current task per sandbox; restart counts. A role that keeps crashing is worth investigating even if tasks are technically still completing via restarts. It's also a problem-detection input (service management design §13).

### 5.14 Settings

Shows notification channels, quiet and business hours, service levels, and risk tolerance. Most of these are **governance configuration items**, so editing one here doesn't change it directly. It creates a system change proposal for the human's own approval, which then goes through the eval and baseline path (change/config design §4.2). Notification channel choice and UI preferences are the exception: they're local operator settings and apply immediately.

---

## 6. Human decision flow, end to end

This is the concrete mechanism behind "only Opus reaches the human" from the escalation design. It's a closed loop, not a one-way notification:

1. Executive Director (T3) determines an escalation matches an `always_human` trigger (governance design §4) and invokes `human_relay`. The engine checks the inbox WIP limit first (core engine design §7.1).
2. `human_relay` hands the packet to Client Communications (T3), which renders it as a **schema-validated human request** with options, recommendation, deadline, default on expiry, and impact of waiting (service management design §3.2). The engine computes **urgency** from the dependency graph and forecast; the agent doesn't declare it.
3. The request becomes an inbox item. A **P1** also sends an operational alert (§4.8); P2/P3 appear in the inbox and digest.
4. The clock adapter drives **reminders, expiry, and reassessment** (§4.3). On expiry, the default is to *park*: affected items wait and everything else continues. Provisional proceeding happens only where the human has opted in for that trigger type (service management design §3.4).
5. The human answers in the inbox from an authenticated session (§10). The monitor sends an `answer_human_request` command to the driver. The resulting record carries the session identity and a driver-held **HMAC**, so it can't be forged or altered afterward (governance design §6.3).
6. The engine publishes the answer as a **decision record** in `canonical_records` (topic `decision.*`), attributed to the human, with the original `escalation_id`. If it overturns a provisional decision, it's published as a superseding record and the engine creates rework for every tagged item.
7. The decision record is distributed through the normal `message_bus` / broker path, not a side channel, to every agent with need-to-know on that topic. Executive Director, having asked the question, is always one of them; others depend on the topic ACL. Parked items return to `ready` automatically.
8. The audit log records the full loop (escalation → framing → alert/reminders → human's raw answer → decision record → distribution), hash-chained and control-tagged, so it's fully reconstructable later.

The important property is unchanged from v0.1: **the human's answer re-enters the system exactly like any other canonical decision.** It uses the same publish and distribute mechanism, the same topic ACL, and the same audit trail. There's no special "human channel" that bypasses need-to-know on the way back out. Only the way *in* (via `human_relay`) is special.

**The other inbox types reuse this loop.** A system change approval signs a `change.*` record and the driver restarts onto the new baseline (§4.6). A production deployment approval releases the deployment command (§4.7). A risk acceptance renewal extends or withdraws a `decision.risk_acceptance.*` record.

---

## 7. Everything the human can change, and how

The monitor offers several write actions, and **none of them writes to a store directly.** Each is a command to the driver, which routes it to the engine, validates it, applies it, and records it like any other state change:

| Human action | Becomes | Path |
|---|---|---|
| Submit idea / new requirement / question | Intake message | `intake_gateway` → Client Comms (§4.4) |
| Drag a card / reprioritize / edit roadmap | Intake message to PO | See below |
| Answer an inbox item | `answer_human_request` | Engine → decision / change / deployment / risk record (§6) |
| Pause (all / role / tier) | `pause` | Process supervisor; low friction (§10.2) |
| Resume | `resume` | Process supervisor; re-auth required |
| Revert to previous baseline | `revert_baseline` | Baseline loader (§4.6); re-auth required |
| Edit a governance setting | System change proposal | Change/config design §4.2, approved by the human in the inbox |
| Run a what-if scenario | Nothing: read-only simulation | §5.4 |

**Roadmap and priority edits** keep v0.1's design. A drag-and-drop reprioritization is packaged by the monitor backend as a message to the Product Owner (`"human requests: move story X above story Y"`), sent through `intake_gateway` exactly as a typed request would be. The PO processes it like any other input. It might just apply it, or it might come back with a question ("moving X ahead of Y means Z slips a PI, confirm?"). That becomes a normal escalation if significant enough, or a direct response if not. This keeps one auditable path for all human influence on the plan, and stops the UI from ever holding backlog state that the canonical record disagrees with. The UI is a client of the intake mechanism, not a second source of truth.

---

## 8. Degraded operation

How the driver and monitor behave in each service mode (service management design §4.2):

| Mode | Driver | Monitor / human |
|---|---|---|
| `local_down` | Router sends T0/T1 work to T2 only where the data class and budget allow; the rest is parked | Banner; alert; cost view shows the overflow spend |
| `t2_down` | Only blocking T2 work is sent to T3, within budget | Banner; alert |
| `t3_down` | T3 jobs queue; **no failover of Opus authority** to T2; intake acknowledged and queued; daily digest still generated | Banner; alert naming queued decisions and the share of work still flowing; inbox shows nothing new until recovery |
| `remote_down` | Local-only operation; referrals and decisions park | Banner; alert |
| `budget_exhausted` | Paid tiers stop | Surfaced as a *decision* (raise the budget or wait), not an incident |
| `paused` | Nothing dispatches; state preserved | Banner; resume requires re-auth |
| **Monitor down** | Engine continues; alerts still sent via notification adapter | Human can't answer; alerts say so |
| **Driver down** | Nothing runs | **External watchdog** (Windows scheduled task, every 5 min) detects stale heartbeats and sends a fixed alert (service management design §6.4) |

---

## 9. Local GPU contention

Since T0/T1 all share the RTX 3060 (escalation design §6.3), the cost and capacity view (§5.8) doubles as the operational view for that contention: queue depth, blocked sandboxes, model swaps, overflow firings, CI load. Two v0.2 additions:

- **Measured, not guessed:** the benchmark harness (service management design §7.2) sets the GPU semaphore and `local_queue_overflow` from measured throughput at concurrency 1/2/3, with and without CI load. The view shows live values against that baseline.
- **Model affinity:** the scheduler prefers jobs for the currently loaded model within a fairness window, and the view shows how many swaps that saved.

---

## 10. Security

v0.1 flagged that anyone on the open, unauthenticated `10.42.0.0/24` LAN could open the dashboard, answer escalations, or hit the kill switch. v0.2 closes that by default.

### 10.1 Access

- **Localhost by default.** The monitor binds to `127.0.0.1`. LAN access has to be switched on explicitly, and then *every* view requires an authenticated session, reads included, because the monitor shows Confidential data (governance design §5).
- **Writes always require a session**, even on localhost, so a local process can't call the write API unauthenticated.
- **Remote access** (away from the host) goes through a reverse proxy or SSH tunnel, never a directly exposed port.

### 10.2 Asymmetric friction

Stopping should be easy; starting or changing should be deliberate:

| Action | Requirement |
|---|---|
| Pause, view anything | Active session |
| Answer escalation, approve deployment | Active session |
| Resume, revert baseline, approve a system change, accept a risk | **Re-authentication** at the time of the action |

### 10.3 Integrity and accountability

- Every human write produces a record with the session identity and a driver-held HMAC (governance design §6.3, 800-53 AU-10).
- The monitor's own actions are audit-logged like everything else, including logins, failed logins, and pauses.
- The driver holds the only T2/T3 credentials (§4.5) and the HMAC key. The **offsite backup key is held by the human**, so a compromised host can't also decrypt its own history (service management design §8.2).

### 10.4 What's worth protecting

The monitor is the only component with read-all visibility across every agent's traffic, and its approval, kill-switch, and revert controls can influence real decisions, spend, and system behavior. The local inference endpoint matters too: it's low-value as compute, but it's where untrusted content meets the models. The endpoint should remain reachable only from the driver's host (escalation design §6.5).

---

## 11. Implementation notes

- **Backend:** FastAPI, serving REST for board and dashboard queries and a WebSocket for the live feed, inbox, and banner updates.
- **Frontend:** a build-light single-page app (plain HTML/JS and a small charting library) served from the same process, so it runs on the same Windows box with no separate build pipeline.
- **Read model:** the backend reads engine **projections**, not the raw event log, for every board. Projections are rebuilt from the log on startup (core engine design §11), so the monitor never holds state of its own that could disagree with the engine.
- **No LLM in the monitor's or driver's own request path**, ever. That's a hard boundary, not a performance choice. Natural-language interaction is provided by an *agent*, not by the monitor: the console's chat is a conversation with Client Communications, whose `converse` job type queries metrics through a deterministic tool (console design §6). The monitor only carries the messages. Otherwise the compartmentalization guarantee in §1 quietly stops being true.
- **Interface construction:** how the monitor's pages are built (trusted core vs. agent-built presentation layer, the NWN-derived shell and style, role dashboards, the console as Product 0) is specified in the console design.
- **Code layout:** `agentco/driver/` and `agentco/monitor/` per core engine design §16. The external watchdog is a separate small script, deliberately outside the driver's codebase and process.

---

## 12. Control mapping

| Framework | Reference | Section |
|---|---|---|
| 800-53 | AC-3 / IA-2 Access enforcement, user authentication | §10.1 |
| 800-53 | AU-2 / AU-9 / AU-10 Logging, audit protection, non-repudiation | §4.8, §10.3 |
| 800-53 | CM-2 / CM-3 / CM-5 Baselines, change control, access restrictions for change | §4.6, §7 |
| 800-53 | CP-9 / CP-4 Backup, contingency testing | §4.9 |
| 800-53 | IR-4 / IR-6 Incident handling and reporting | §4.8, §5.10 |
| 800-53 | SI-4 System monitoring | §5, §8 |
| SSDF | PS.1 Protect code (sole committer, signed commits) | §4.7 |
| AI RMF | MANAGE 4 Post-deployment monitoring and response | §5.9–5.12, §8 |
| AI 600-1 | Human-AI configuration (decision log review, alert fatigue controls) | §4.8, §5.6 |
| ITIL 4 | Monitoring & event mgmt, Service desk, Change enablement, Release/Deployment mgmt, Service continuity | §4, §5, §8 |

---

## 13. Open questions / follow-ups

**Resolved in v0.2:**

| v0.1 question | Resolution |
|---|---|
| Notification delivery | Notification adapter + operational alerts (§4.8) |
| Multi-sandbox-per-role scheduling | Org chart `instances` + engine scheduler capacity (§4.2; core engine design §7) |
| Auth for the monitor UI | Localhost default, authenticated sessions, re-auth for high-impact actions (§10) |
| Historical replay | Engine event log replay (§5.7) |

**Still open:**
- **Notification channel choice:** which channel reaches the human away from the PC (service management design §18).
- **Session mechanism:** a local password + session cookie is enough for localhost. LAN or remote use may warrant a hardware key or OS-integrated auth (Windows Hello). Decide before LAN access is enabled.
- **What-if scenario scope** (§5.4): which parameters the human can vary without the UI turning into a second config editor. Start with scope, budget, and instance counts.
- **Mobile inbox:** answering P1 requests away from the PC implies remote access (§10.1). That's the point where the reverse-proxy and auth decisions stop being theoretical.
