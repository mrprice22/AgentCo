# AgentCo — Monitor/Driver Design

**Design document v0.1**
**Scope:** The one component in this system that is *not* an agent — the process the human actually runs, watches, and occasionally talks back to. Covers how it supervises agent sandboxes, how it surfaces backlog/sprint/roadmap state, and how human decisions (from the escalation design) actually reach the human and get relayed back.

---

## 1. What this program is and isn't

It's one program with two logical halves, which matters enough to name separately even though they ship together:

- **Driver** — the control plane. Starts and supervises agent sandbox processes, runs the ceremony scheduler (standups, sprint boundaries), is the entry point for the human's original idea, and is the delivery mechanism for anything Claude Opus decides needs the human (§6).
- **Monitor** — the observation plane. Read-only views over backlog, sprint board, roadmap/release schedule, live agent activity, cost, and compliance status.

**It is deliberately not an agent.** It has no role in the org chart, no tier, no model backend, and no escalation ladder. This matters for the compartmentalization design in particular: the whole point of need-to-know enforcement (data layer doc, §5) is to stop *agents* from accumulating a full picture they haven't earned the right to. The human already sits above that hierarchy by construction — they're the client the whole company serves — so the monitor is explicitly exempt from the broker's ACL and reads everything. This is a privilege that must never leak to an agent: the monitor cannot be prompted, cannot answer questions, and has no LLM in its own execution path. It's plumbing, not a participant.

---

## 2. Architecture overview

```
┌───────────────────────────────────────────────────────────────────────┐
│                              DRIVER                                    │
│  ┌─────────────────┐  ┌────────────────────┐  ┌─────────────────────┐ │
│  │ process_supervisor│  │ ceremony_scheduler │  │ intake_gateway       │ │
│  │ starts/restarts   │  │ standup/sprint/    │  │ human's raw idea →   │ │
│  │ agent sandboxes    │  │ retro on schedule  │  │ Client Comms intake  │ │
│  └─────────┬────────┘  └──────────┬─────────┘  └──────────┬──────────┘ │
│            │                       │                        │           │
│            └───────────────────────┴────────────┬───────────┘           │
│                                                    │  (all via message_bus)
└────────────────────────────────────────────────────┼───────────────────┘
                                                       │
                                          ┌────────────▼────────────┐
                                          │      message_bus         │
                                          │  (from prior designs)    │
                                          └────────────┬────────────┘
                                                        │ read-only tap
                                                        │ (bypasses broker ACL —
                                                        │  monitor sees everything)
┌───────────────────────────────────────────────────────┼───────────────┐
│                              MONITOR                    │               │
│  ┌───────────────┐  ┌───────────────┐  ┌───────────────▼────────────┐ │
│  │ event_tap      │  │ backend (API) │  │ web UI (browser, on LAN)   │ │
│  │ subscribes to   │─▶│ REST + WS to  │─▶│ boards, feed, inbox,      │ │
│  │ every message   │  │ the frontend  │  │ cost/compliance dashboards│ │
│  └───────────────┘  └───────┬───────┘  └────────────────────────────┘ │
│                              │ reads                                    │
│                    ┌─────────▼─────────┐                                │
│                    │ canonical_records  │  (backlog, sprint, roadmap,   │
│                    │ + audit_log         │   decisions — from data layer│
│                    │ (data layer stores) │   and escalation designs)    │
│                    └────────────────────┘                                │
└───────────────────────────────────────────────────────────────────────┘
```

The monitor never writes to an agent's private memory (episodic_log/semantic_notes stores) — those stay agent-private per the data layer design, and the monitor has no need to see them anyway; everything a human needs to track (backlog, sprints, roadmap, decisions, escalations) lives in `canonical_records` or the audit log, both already designed to be the shared, durable record.

---

## 3. Data sources

| View needs | Reads from |
|---|---|
| Backlog / Kanban board | `canonical_records` (topics `epic.*`, `story.*`, `task.*`) + live status deltas off the event tap |
| Sprint board | Same, filtered to the current sprint window (`agile.yaml` sprint config) |
| Roadmap / release schedule | `canonical_records` (topics `roadmap.*`, `decision.release.*`) |
| Escalation inbox (pending) | A dedicated `pending_human_decisions` table, written only by the human-relay tool (§6) |
| Decision log (resolved, for transparency) | `audit_log` entries where `resolution_tier` is T3 and no human was actually asked — so the human can review what Opus decided *without* being asked, building trust in the "most things don't need you" design |
| Live activity feed | `event_tap` — a live subscription to `message_bus`, display-only, never re-injected |
| Cost / tier dashboard | `audit_log` aggregated by tier (token counts, wall-clock, and $ for T2/T3 calls) + a live GPU-queue-depth reading from the local router (§8) |
| NIST compliance status | `audit_log` entries tagged with SSDF practice IDs (from the Security Reviewer's gate outputs) and AI RMF entries from the compliance mapper |
| Agent health | `process_supervisor`'s own liveness table (last heartbeat, restart count, current task per agent process) |

The monitor's backend never writes to `canonical_records` directly except in one case: recording the human's answer to an escalation (§6.3), which is the one place the human's own authority enters the system's durable record.

---

## 4. Driver responsibilities

### 4.1 Process supervision

`driver/process_supervisor.py` owns the lifecycle of every agent sandbox process:

- Starts one sandbox per active role-instance (a busy sprint may have several Developer sandboxes running concurrently against different tasks — see §8 for how this interacts with local GPU contention).
- Health-checks via heartbeat; restarts a crashed sandbox with the same task re-queued (task state itself lives in `canonical_records`/backlog store, not in the sandbox, so a restart doesn't lose work — it just re-reads its assigned task and its own semantic_notes store, which also survives the crash).
- Exposes current process state (`running | restarting | crashed | idle`) per agent to the monitor's Agent Health view.
- Owns the **kill switch**: given the open, unauthenticated LAN and the fact that T2/T3 calls cost real money, the supervisor exposes a single "pause all" / "pause role X" / "resume" control, reachable only from the monitor UI (not from any agent — no agent has a tool that touches the supervisor).

### 4.2 Ceremony scheduler

`driver/ceremony_scheduler.py` reads `agile.yaml` (sprint length, ceremony cadence) and, on schedule, dispatches the relevant ceremony task to Scrum Master (standup, sprint planning, retro, backlog grooming) exactly as any other task would be dispatched — the scheduler's only special property is that it's time-triggered rather than triggered by a preceding message.

### 4.3 Intake gateway

`driver/intake_gateway.py` is where the human's original idea enters the system, and later where any human-initiated mid-project input arrives (a new requirement, a reprioritization request, an answer to a pending escalation). It always enters as a message to **Client Communications** — never as a direct write to any agent's memory or to `canonical_records` — which keeps the "human input always flows through the same one door" property intact even for things the UI makes feel like a direct edit (e.g., dragging a backlog card — see §7).

### 4.4 CLI-backed tier interface

T2 (DeepSeek-V4-pro via OpenCode) and T3 (Claude Opus via the `claude` CLI) are invoked as subprocesses from the driver's model-backend layer (extends `model_backends/router.py` from the escalation design). The driver:

- Manages subprocess lifecycle (spawn, timeout, capture stdout/stderr, clean shutdown).
- Parses CLI output back into the same structured envelope every other tier uses, so nothing downstream needs to know a given response came from a CLI subprocess versus an HTTP call.
- Captures whatever token/cost accounting each CLI exposes and logs it to `audit_log` for the cost dashboard (§3, §8). Where a CLI doesn't expose token counts directly, the driver falls back to estimating from prompt/response length.

---

## 5. Monitor views

### 5.1 Backlog board (Kanban)

Columns by status (Backlog → In Progress → Review → Done) across Epics/Stories/Tasks, matching the `backlog/models.py` hierarchy from the repo design. Clicking a card shows its acceptance criteria, current owner (role + tier + which model), and — critically — its **escalation history**, if any: a card that bounced T0→T1→T2 before resolving is visibly different from one that sailed through, which is useful for spotting systemically under-specified work (ties back to the tuning loop in the escalation design, §8 of that doc).

### 5.2 Sprint board

The same data, scoped to the active sprint window, with burndown computed from story points and days elapsed (`agile.yaml` config). This is the view the Scrum Master's standup/planning output populates.

### 5.3 Roadmap / release schedule

A timeline view: epics laid out across sprints, with target/actual completion. This is *rendered from* what Product Owner and Executive Director have published to `roadmap.*` — the monitor doesn't compute the roadmap itself, it visualizes the canonical version. See §7 for how the human edits it.

### 5.4 Escalation inbox

The one place the human is asked to act. Shows every `pending_human_decisions` row not yet answered, rendered via whatever Client Communications wrote as the human-facing framing (per its prompt design — plain language, small number of clear options). Answering here is the only "write" surface exposed to the human besides roadmap edits and process controls.

### 5.5 Decision log (resolved without you)

Deliberately placed next to the inbox, not buried: every decision Opus made *without* escalating, with its rationale, so the human can audit "was I right to trust it with that" after the fact and recalibrate the `human_escalation_triggers` config (§6 of the escalation doc) if Opus is either escalating too much or deciding things it shouldn't have.

### 5.6 Live activity feed

A scrolling, filterable view of the raw message bus (via `event_tap`) — every message between every agent, in real time. Filterable by role, task, or topic. This is the "watch it evolve" view: seeing a Developer task go out, come back needs_clarification, escalate, and resolve, in the order it actually happened, is a fundamentally different (and more trust-building) experience than only ever seeing final states on the Kanban board.

### 5.7 Cost & tier dashboard

Running totals: calls and tokens per tier, $ spent on T2/T3 today/this sprint, local GPU queue depth and average local latency, and proximity to `daily_spend_limit` (the open item flagged in the escalation design — this view is where that budget alarm actually surfaces, and per that design, approaching the limit is itself a T3-only human-escalation trigger).

### 5.8 NIST compliance dashboard

SSDF gate pass/fail rates by practice ID, any open findings from Security Reviewer, and an AI RMF summary (Govern/Map/Measure/Manage evidence counts) pulled from the compliance mapper's log entries — this is the artifact a human would actually hand to an auditor.

### 5.9 Agent health

Which sandboxes are running, idle, restarting, or crashed; current task per sandbox; restart counts (a role that keeps crashing is worth investigating even if tasks are technically still completing via restarts).

---

## 6. Human decision flow, end to end

This is the concrete mechanism behind "only Opus reaches the human" from the escalation design, and it matters that it's a closed loop, not a one-way notification:

1. Executive Director (T3) determines an escalation packet matches a `human_escalation_trigger` and invokes `human_relay`.
2. `human_relay` hands the packet to Client Communications (T3) to render as a plain-language question with a small set of options (per its prompt design).
3. The rendered question is written to `pending_human_decisions` (not delivered as a push notification by default in v0.1 — the human is expected to have the monitor open or check in; a future revision could add an OS notification or webhook).
4. The Escalation Inbox view (§5.4) shows it. The human answers via the UI.
5. The driver writes the answer as a **decision record** into `canonical_records` (topic `decision.*`), attributed to the human, with the original `escalation_id` for traceability.
6. The driver sends this decision record as a message — through the normal `message_bus` / broker path, not a side channel — to whichever agents have need-to-know on that topic. Executive Director, having asked the question, is always one of them; others depend on the topic ACL.
7. The audit log records the full loop: escalation → framing → human's raw answer → decision record → distribution — so this is fully reconstructable later.

The important property: **the human's answer re-enters the system exactly like any other canonical decision would** — same publish/distribute mechanism, same topic ACL, same audit trail. There's no special "human channel" that bypasses need-to-know on the way back out; only the way *in* (via `human_relay`) is special.

---

## 7. Roadmap and priority edits

The monitor lets the human drag a card, reprioritize the backlog, or edit the roadmap directly in the UI — but this is presentation sugar over the same intake path, not a direct database write:

- A drag-and-drop reprioritization is packaged by the monitor backend as a message to Product Owner (`"human requests: move story X above story Y"`), sent through `intake_gateway` exactly like a typed request would be.
- Product Owner processes it like any other input — it might just apply it, or it might come back with a clarifying question ("moving X ahead of Y means Z slips a sprint, confirm?") which becomes a normal escalation if it's significant enough to meet a Product-Owner-level threshold, or a direct response if not.
- This keeps a single, auditable path for all human influence on the plan, and stops the UI from ever holding backlog state that the agents' canonical record disagrees with — the UI is a client of the same intake mechanism, not a second source of truth.

---

## 8. Local GPU contention, visualized

Since T0/T1 all share the RTX 3060 (per the escalation design, §6.3 of that doc), the monitor's cost dashboard doubles as the operational view for that contention: current queue depth for local inference, which sandboxes are currently blocked waiting on a local slot, and how often the `local_queue_overflow` escape valve (routing T0 work to T2 under load) has fired. This is the practical instrument for tuning `local_queue_overflow` and the concurrency semaphore mentioned as an open question in that design — the number is otherwise invisible.

---

## 9. Security considerations

Given the host is on an open, unauthenticated `10.42.0.0/24` LAN:

- The monitor's web UI binds to that LAN by default, which means **anyone on the LAN can currently open the dashboard and answer escalations or hit the kill switch** — there's no login. This is acceptable for a single-human, single-LAN setup but should be flagged explicitly rather than silently assumed safe: if this LAN is ever shared (guest devices, IoT, etc.), the monitor should get at minimum a shared-secret cookie or be bound to localhost with an SSH tunnel/reverse proxy for remote access, before it's treated as production-safe.
- The monitor is the only component with read-all visibility across every agent's traffic — which makes it, along with the local inference endpoint, one of the two things on this network that actually matter to protect. Unlike the inference endpoint (which is low-value to an attacker — it's just compute), the monitor's escalation-answer and kill-switch controls are high-value, since they can influence real decisions and spend.

---

## 10. Implementation notes

- **Backend:** a lightweight Python web framework (FastAPI is a natural fit given the rest of the stack is already Python) serving REST for board/dashboard queries and a WebSocket for the live feed and inbox push updates.
- **Frontend:** a single-page app served from the same process; given this is a LAN-local tool for one human, a build-light approach (plain HTML/JS/a small charting lib, no heavy SPA framework) keeps it easy to run on the same Windows box without a separate build pipeline.
- **No LLM in the monitor's own request path**, ever — this is a hard boundary, not a performance choice. If a future version wants a natural-language "ask the dashboard a question" feature, that has to be implemented as a *new agent role* with its own tier and need-to-know scope, not as a shortcut inside the monitor itself, or the compartmentalization guarantee in §1 quietly stops being true.

---

## 11. Open questions / follow-ups

- **Notification delivery:** v0.1 assumes the human checks the inbox proactively; a push notification (desktop toast, webhook, etc.) for pending escalations is a natural follow-up once the polling-vs-open-tab tradeoff is felt in practice.
- **Multi-sandbox-per-role scheduling:** §4.1 assumes multiple concurrent sandboxes per role are possible, but doesn't yet specify how `process_supervisor` decides how many to spin up for a given backlog depth — likely bounded by the same local GPU contention limits from the escalation design.
- **Auth for the monitor UI** (§9) is flagged but not designed — worth resolving before this leaves a single-human/single-LAN context.
- **Historical replay:** the live activity feed (§5.6) is real-time only in v0.1; being able to scrub back through a completed sprint's message history for a retro would reuse the same `event_tap` data if persisted, but that persistence isn't specified yet.
