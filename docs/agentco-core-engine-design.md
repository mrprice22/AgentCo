# AgentCo — Core Business Simulation Engine

**Design document v0.1**
**Scope:** The engine that makes the simulated company *run*. Covers:
- the org model (who exists)
- the work model (what gets done)
- the state machines that move work from idea to done
- scheduling against scarce capacity (GPU, budget, human attention)
- the per-invocation job cycle
- the time model
- event-sourced state and replay
- a simulation mode for forecasting and what-if analysis without spending tokens

This is the "repo design" the escalation design refers to (`org_chart.yaml`, `task_bounds.yaml`, `backlog/models.py`). It's written down here for the first time, and other documents should treat this one as the owner of those files.

---

## 1. What the engine is

The other designs each cover one piece: routing (escalation), memory and access (data layer), supervision and observation (monitor/driver), and rules (governance, change/config). None of them owns the **business logic of the company**: what a story is, when a task is ready, who acts next, what happens to a Tester's "fail" verdict, when a sprint ends. The engine owns that.

Three properties define it:

1. **Deterministic, with no LLM inside.** The engine is a state machine plus a scheduler. It *invokes* agents; it's never one. Given the same event history it always reaches the same state (§11). This is the same boundary the monitor design draws for itself (§11 of that doc), for the same reason: anything that enforces rules must not be promptable.
2. **Agents propose, the engine disposes.** No agent mutates state. Every agent output is a *proposal* (a diff, a verdict, a backlog change, a decision). The engine validates it against schema, authority, and policy, then applies its effects (§8). An agent that says "mark story 88 done" has asked; only the engine can do it.
3. **I/O through ports, logic in the core.** The engine core never touches a socket, a file, a model, or a clock directly. It talks to *ports* (message bus, model router, sandbox pool, repo, CI, clock, event store). Real adapters make it run the company; simulated adapters make it *simulate* the company (§12). The same code serves both.

**Relationship to the driver** (monitor/driver design §4): the driver is the *host process*. It supervises sandboxes, runs the ceremony timer, accepts intake, and provides the real adapters. The engine is the *domain core* running inside it. The ceremony scheduler, intake gateway, and human relay become thin adapters that turn outside events into engine events.

---

## 2. Architecture

```
                         ┌────────────────────────────────────────────┐
   intake_gateway ──────▶│                ENGINE CORE                  │
   ceremony timer ──────▶│  (pure: no I/O, no LLM, deterministic)      │
   human inbox answers ─▶│                                              │
   CI / gate results ───▶│  ┌───────────┐ ┌───────────┐ ┌───────────┐ │
   job results ─────────▶│  │ org model │ │ work model│ │ workflow  │ │
                         │  │ (§3)      │ │ (§4)      │ │ FSMs (§5) │ │
                         │  └───────────┘ └───────────┘ └───────────┘ │
                         │  ┌───────────┐ ┌───────────┐ ┌───────────┐ │
                         │  │decomposer │ │ scheduler │ │ effects   │ │
                         │  │+ bounds(§6)│ │ (§7)      │ │ appliers  │ │
                         │  └───────────┘ └───────────┘ │ (§8)      │ │
                         │                               └───────────┘ │
                         │        in: events  ──▶  out: commands        │
                         └───────────────────────┬─────────────────────┘
                                                 │ ports
       ┌──────────────┬──────────────┬───────────┼──────────┬──────────────┬─────────────┐
       ▼              ▼              ▼           ▼          ▼              ▼             ▼
  EventStore      MessageBus    ModelRouter  SandboxPool  RepoAdapter   CIAdapter      Clock
  (§11)           +broker       (escalation  (driver      (change/cfg   (change/cfg   (§10)
                  (data layer)   design)      supervisor)  §8)           §8.3)
       │              │              │           │          │              │             │
   ── real adapters run the company ─┴── sim adapters simulate it (§12) ─┴─────────────┘
```

The core consumes **events** (`job.returned`, `gate.passed`, `human.answered`, `timer.iteration_ended`, …) and emits **commands** (`dispatch_job`, `publish_record`, `commit_diff`, `request_human_decision`, …). Adapters carry out commands and report outcomes back as new events. That loop is the whole engine.

---

## 3. Org model

### 3.1 `org_chart.yaml`

The org chart declares every role, what jobs it can do, and how its problems get routed. It's a governance configuration item (change/config design §2).

```yaml
roles:
  developer:
    prompt: agentPrompts/developer.jinja
    output_schema: schemas/developer.output.json
    instances: { min: 1, max: 4 }          # sandbox count, bounded by capacity (§7)
    tools_allowed: []
    job_types:
      implement_task: { default_tier: T0, ladder: [T0, T1, T2, T3] }
    referrals:                              # §9: "not my call" routes to another role
      ambiguous_acceptance_criteria: product_owner
      scope_violation: architect
      design_decision_needed: architect

  architect:
    prompt: agentPrompts/architect.jinja
    output_schema: schemas/architect.output.json
    instances: { min: 1, max: 1 }
    job_types:
      decompose_story: { default_tier: T0, ladder: [T0, T1, T2] }   # high-volume: keep it cheap
      decide:          { default_tier: T1, ladder: [T1, T2, T3] }   # "one hard question"
      review_design:   { default_tier: T1, ladder: [T1, T2] }
    referrals:
      business_tradeoff: product_owner

  product_owner:
    job_types:
      refine_backlog:   { default_tier: T2, ladder: [T2, T3] }
      answer_referral:  { default_tier: T2, ladder: [T2, T3] }
    referrals:
      beyond_po_authority: executive_director
  # … tester, security_reviewer, scrum_master, executive_director, client_communications
```

### 3.2 Job types separate *what* from *how hard*

The escalation design assigns a tier per role. In practice, one role does jobs of very different difficulty. Architect story decomposition happens once per story and is fairly mechanical. An Architect cross-cutting decision is rare and hard. Running both at T1 (~4 tok/s) would make decomposition the bottleneck for the whole company. **Tier is set per job type, not per role.** `models.yaml` keeps model definitions and routing rules; `org_chart.yaml` says which job types start at which tier.

### 3.3 Two kinds of "escalation": tier vs. referral

The prompt templates currently mix two different things under "escalate":

- **Tier escalation.** *The same job, retried by a stronger model.* The role doesn't change. Triggers: retry exhaustion, low confidence, capability limits. This is the ladder in the escalation design.
- **Referral.** *The question isn't this role's to answer.* It goes to the role that owns it, at that role's default tier for `answer_referral`. Triggers: ambiguous acceptance criteria (goes to the PO), scope violation (goes to the Architect to re-decompose), business tradeoffs (goes to the PO), beyond PO authority (goes to Exec Director).

Escalating a question about ambiguous acceptance criteria up the *tier* ladder just pays a smarter model to guess at the Product Owner's intent. It needs a *referral*. §9 gives the engine's mapping from trigger to action, and the worker contract gains a field so agents can say which one they mean (§8.2).

---

## 4. Work model

### 4.1 Hierarchy

```
Portfolio
 └─ Epic                 (Lean business case; epic owner = Executive Director)
     └─ Feature          (fits in one PI; benefit hypothesis; owned by Product Owner)
         └─ Story        (fits in one iteration; acceptance criteria; owned by Product Owner)
             └─ Task     (one job for one worker; must satisfy task_bounds — §6)
```

**Feature** is added between Epic and Story to align with SAFe (the full SAFe mapping is in the SAFe/PMO design, to follow). Epics, Features, and Stories are each either **business** or **enabler** (architecture, infrastructure, compliance, exploration). Enablers are how the Architect's runway work and the governance design's threat models (§9, PW.1) get into the backlog as real, scheduled work instead of side effects.

### 4.2 Work item schema (`engine/work_items.py`, formerly `backlog/models.py`)

```python
@dataclass(frozen=True)
class WorkItem:
    id: str                       # "story_88"
    kind: Literal["epic", "feature", "story", "task"]
    nature: Literal["business", "enabler"]
    parent_id: str | None
    title: str
    description: str
    acceptance_criteria: list[str]
    nfrs: list[str]               # non-functional requirements inherited down the tree
    state: str                    # §5.1
    owner_role: str
    assignee: str | None          # sandbox_id when in progress
    wsjf: Wsjf | None             # §7.2 — required on epic/feature, inherited below
    size_points: int | None
    iteration_id: str | None
    depends_on: list[str]
    relevant_files: list[str]     # tasks only
    data_class: str               # governance design §5 — derived from topics touched
    budget_usd: Decimal | None    # epics/features: Lean budget guardrail (§13)
    version: int                  # optimistic concurrency (§14)
```

### 4.3 Readiness and doneness

**Definition of Ready** and **Definition of Done** are config (`workflow.yaml`), checked by the engine rather than asserted by an agent:

```yaml
definition_of_ready:
  story: [has_acceptance_criteria, has_size, wsjf_inherited, no_open_referrals, dependencies_scheduled]
  task:  [within_task_bounds, relevant_files_exist, parent_story_ready]
definition_of_done:
  task:  [ci_green, tester_pass, security_gate_pass, merged]
  story: [all_tasks_done, story_level_acceptance_test_pass]
  feature: [all_stories_done, demoed]           # System Demo (SAFe design)
```

### 4.4 Other kinds of work

Not everything is backlog. The engine tracks these as work items with their own state machines, so they're scheduled, measured, and replayable the same way:

| Kind | Created by | Lifecycle owner |
|---|---|---|
| `escalation` | Any job trigger (§9) | Engine, per escalation design packet |
| `decision` | PO / Architect / Exec Dir / human | Engine → canonical record |
| `ceremony` | Iteration/PI timer (§10) | Scrum Master jobs |
| `change` | Change/config design §5 | Engine, per change lifecycle |
| `human_request` | `request_human_decision` command | Engine + monitor inbox |
| `incident` | Driver / monitor (service management design) | Engine |

---

## 5. Workflow state machines

### 5.1 Work item states (story / task)

```
 draft ──▶ ready ──▶ in_progress ──▶ in_review ──▶ in_test ──▶ done
   ▲         │            │  ▲            │            │
   │         │            ▼  │            ▼            ▼
   │         │         blocked ◀──────────┴──── (rework: back to in_progress)
   │         ▼            │
   └──── needs_refinement ◀┘  (referral returned "criteria changed" / re-decompose)

 any ──▶ cancelled        (PO or Exec Dir only)
 any ──▶ awaiting_human   (a human_request blocks it; returns to prior state on answer)
```

### 5.2 Transition authority

Every transition names who may cause it and what guard must hold. Agents trigger transitions only indirectly, through proposals the engine accepts.

```yaml
# workflow.yaml (excerpt)
transitions:
  - { kind: task, from: draft,       to: ready,       by: engine,   guard: definition_of_ready.task }
  - { kind: task, from: ready,       to: in_progress, by: engine,   guard: scheduler_assigned }
  - { kind: task, from: in_progress, to: in_review,   by: engine,   guard: diff_committed_in_scope }
  - { kind: task, from: in_review,   to: in_test,     by: engine,   guard: ci_green and security_gate_pass }
  - { kind: task, from: in_test,     to: done,        by: engine,   guard: tester_pass and merged }
  - { kind: task, from: in_test,     to: in_progress, by: engine,   guard: tester_fail and rework_count < rework_limit }
  - { kind: story, from: any,        to: cancelled,   by: [product_owner, executive_director] }
  - { kind: story, from: draft,      to: ready,       by: engine,   guard: definition_of_ready.story }
```

A Tester "fail" is **rework**, not escalation (as the Tester prompt says). Rework loops get a limit (`rework_limit`, default 3). Hitting it fires a new trigger, `rework_exhaustion`, which becomes a referral to the Architect: repeated fail/fix cycles usually mean the task, or its acceptance criteria, is wrong rather than the code.

### 5.3 Job states (one agent invocation)

```
 queued ──▶ leased ──▶ running ──▶ returned ──▶ validated ──▶ applied
                          │            │             │
                          ▼            ▼             ▼
                      timed_out    schema_invalid  rejected (authority/policy)
                          │            │             │
                          └────────────┴──── retry (≤ max_local_retries) ──▶ queued
                                              └──── exhausted ──▶ escalation (§9)
 poison (fails across all tiers) ──▶ dead_letter + incident
```

---

## 6. Decomposition and task bounds

### 6.1 The pipeline from idea to task

| Step | Who | Output | Engine check |
|---|---|---|---|
| 1. Intake | Client Communications (T3) | Faithful capture of the idea, with gaps flagged | Creates `idea` record; gaps become `human_request`s only via Exec Dir |
| 2. Vision | Executive Director (T3) | Vision record + candidate epics | Publish authority on `vision` |
| 3. Epics → Features → Stories | Product Owner (T2) | Backlog changes with acceptance criteria and WSJF inputs | Schema; DoR for stories |
| 4. Enablers | Architect `decide` / `review_design` (T1) | Architecture records, enabler stories, threat model per epic | Publish authority on `architecture.*` |
| 5. Story → Tasks | Architect `decompose_story` (T0) | Tasks with `relevant_files`, acceptance criteria subset, dependencies | **`task_bounds` (§6.2) — rejected decompositions go back with specifics** |

### 6.2 `task_bounds.yaml`

Bounds are what make T0 workable. They're enforced when a task is *created*, not discovered when it fails.

```yaml
task_bounds:
  implement_task:
    max_relevant_files: 3
    max_estimated_new_loc: 200
    max_acceptance_criteria: 3
    max_context_tokens: 10000     # prompt + retrieved notes + file contents
    max_output_tokens: 3000
  test_task:
    max_artifact_tokens: 10000
```

**Context-fit rule.** For every tier on the job type's ladder, at least one model at that tier must fit `max_context_tokens + max_output_tokens` plus prompt-template overhead plus the escalation packet that tier would receive (escalation design §4.3, §6.2). T1's 16K window is the binding limit for ladders that include it, which is why the example above is 10K/3K. The engine checks this at config load and refuses to start with a bound that can't fit. At dispatch, the router only chooses models whose context fits the assembled prompt, which fixes v0.1's routing bug (inputs over 10K tokens sent to the model with the *smaller* 16K window).

---

## 7. Scheduling and capacity

### 7.1 Resources

The engine schedules against four scarce resources. Each has one owner, and the engine asks rather than assumes:

| Resource | Owner | Unit |
|---|---|---|
| Local inference slots | Router's GPU semaphore (escalation design §6.3) | Concurrent local jobs |
| Sandbox instances | Process supervisor | Per-role `instances.max` |
| Remote budget | Cost ledger (§13) | $ vs. daily/sprint/epic guardrails |
| **Human attention** | Monitor inbox | Open `human_request` items |

**Human attention has a WIP limit** (`human_inbox_wip`, default 3). When it's reached, Exec Dir jobs that would raise *another* human request are told so in their prompt context. They must decide within their authority, batch the request with an open one, or queue it at lower priority. This puts the escalation design's goal of reserving human attention into the scheduler instead of leaving it to prompt wording alone.

### 7.2 Priority

The ready queue is ordered by:

1. **Unblocking value:** jobs whose completion unblocks other in-progress work, including answering referrals and escalations, go first. Waiting work is waste.
2. **WSJF** of the owning feature: `(business_value + time_criticality + risk_reduction_opportunity_enablement) / job_size`. The PO supplies the inputs; the engine computes the score, so prioritization is repeatable and auditable instead of a free-text judgment.
3. **Age**, as a tiebreaker to prevent starvation.

### 7.3 WIP limits

Per state and per role, from `workflow.yaml` (e.g., `in_progress` ≤ number of developer instances; `in_review` ≤ 5). A full downstream state stops upstream dispatch, which keeps a slow Tester or Security Reviewer from building up a pile of unreviewed diffs.

---

## 8. The job cycle

### 8.1 Steps

Every agent invocation, for every role, follows the same cycle:

1. **Lease:** the scheduler assigns a queued job to an idle sandbox of the right role and acquires resources (§7.1).
2. **Assemble context:** the task payload, plus the agent's own `semantic_notes` retrieved by topic (data layer design §7.1), plus canonical records the broker has already delivered to that agent. **Never** anything the agent's need-to-know doesn't cover. Each segment is labeled with its origin and data class (governance design §7).
3. **Render:** the prompt template at the **baseline** version (change/config design §3), with thresholds from `risk_tolerance.yaml`.
4. **Route:** hand off to the router with job type, tier, data class, and size. The router picks the model (escalation design §6; governance design §5.2 ceiling check).
5. **Validate:** check the output schema, then authority (can this role propose this effect?), then policy (bounds, data class, budget).
6. **Apply effects:** role-specific appliers (§8.3) turn the validated proposal into engine events and commands.
7. **Write-back:** the agent's end-of-task note step (data layer design §6).
8. **Release:** release resources, record cost (§13), emit `job.applied`.

### 8.2 Worker contract addition

The worker contract (escalation design §7) gains one field so agents can say *what kind* of help they need. The engine then decides tier escalation versus referral deterministically (§9):

```json
{
  "status": "needs_clarification",
  "escalation": {
    "reason": "ambiguous_acceptance_criteria",
    "question": "Does 'handle guests appropriately' mean guest checkout is allowed?",
    "blocking": true
  },
  "confidence": 0.35
}
```

`reason` is an enum defined per role in `org_chart.yaml` (the keys of `referrals`, plus the standard tier-escalation reasons). Free-text reasons are rejected as a schema failure.

### 8.3 Effect appliers

| Role (job type) | Proposal | Engine effect |
|---|---|---|
| Client Comms (intake) | Captured idea + flagged gaps | Create `idea`; dispatch Exec Dir `set_vision` |
| Exec Director | Decision / vision / "human needed" | Publish canonical record, or create `human_request` (§7.1 WIP check) |
| Product Owner | `backlog_changes` | Create/update epics, features, stories (version-checked); publish `decision.*`; recompute WSJF order |
| Architect (`decompose_story`) | Task list | Bounds check → create tasks in `draft` → DoR → `ready`; or return rejection details |
| Architect (`decide`) | Architecture decision | Publish `architecture.*` (supersedes prior version) |
| Developer | `diff_or_code`, `files_touched` | `commit_diff` command → driver checks scope and commits (change/config design §8.2) → CI |
| Tester | Verdict + failures | `in_test → done`, or rework with failures attached to the next Developer job |
| Security Reviewer | Gate result | `gate.passed` / `gate.failed` / security-fast-path escalation |
| Scrum Master | Ceremony summary, routed blockers | Board annotations only (can't change priority or state); blockers become referrals |

---

## 9. Escalation and referral handling

The engine maps `(role, trigger/reason)` to an action. Agents never choose the destination.

| Trigger / reason | Action | Destination |
|---|---|---|
| `retry_exhaustion`, `low_confidence`, `schema_invalid` × N | **Tier escalation** | Next rung of the job type's ladder |
| `security_flag` | Tier escalation, skip T1 | T2 (security fast path, escalation design §6.4) |
| `data_class_ceiling` | Tier escalation, skip ineligible tiers | Next tier whose ceiling allows the data class (governance design §5.2) |
| `ambiguous_acceptance_criteria` | **Referral** | Product Owner (`answer_referral`) |
| `scope_violation`, `rework_exhaustion` | Referral | Architect (`decompose_story` in re-decompose mode) |
| `design_decision_needed`, `conflicting_architecture` | Referral | Architect (`decide`) |
| `business_tradeoff` | Referral | Product Owner |
| `beyond_po_authority` | Referral | Executive Director |
| Anything at T3 matching `always_human` | `human_request` | Human via Client Comms framing (monitor design §6) |

**Loop guards:**
- A referral carries a `referral_chain`. The same question returning to a role it already visited, or a chain longer than 4 hops, is forwarded to Exec Director with the full chain. That's usually a sign two roles each think the other owns the question.
- An item can hold at most one open referral at a time.
- The original job is parked in `blocked` with a pointer to the referral, and resumes automatically when the referral's answer is published.

---

## 10. Time model

A simulated company doesn't work at human speed, and forcing it onto human sprint lengths wastes most of its throughput. But the human does live at human speed. So the engine runs **two cadences**:

| Cadence | Length (default) | Ceremonies | Audience |
|---|---|---|---|
| **Iteration** (agent cadence) | 1 wall-clock day, or when committed scope completes, whichever is first | Planning, daily standup-equivalent every N jobs, retro | Agents |
| **Program Increment** (human cadence) | 1 week (5 iterations) | PI planning review, System Demo, Inspect & Adapt, governance review (governance design §13) | Human + Exec Dir + Client Comms |

`agile.yaml` sets both. The human-facing commitments in the service management design (report cadence, response expectations) run on the PI cadence and wall-clock time. Agent ceremonies run on iteration boundaries.

**Clocks are a port.** The real adapter reads wall-clock time. The simulation adapter (§12) is a virtual clock that jumps straight to the next scheduled event, so simulating a quarter takes seconds.

**Run state.** The owner's **Start / Pause** control (monitor design §4.2) gates the whole company: while paused, the scheduler leases nothing, no model is called, and the iteration and PI clocks stop. A naming note: the owner calls running the company *the business simulation*. That's the real, model-backed run. *Simulation mode* (§12) is the separate forecasting sandbox with stub agents; it never calls a model and isn't affected by Start / Pause.

---

## 11. State, events and replay

### 11.1 Event sourcing

The engine's system of record is an **append-only event log**. Every state in §5 is a projection of it (boards, queues, burndown), rebuilt by folding events, with periodic snapshots for fast startup.

```json
{
  "seq": 18421,
  "event_id": "evt_c0a1...",
  "type": "job.returned",
  "aggregate_id": "task_1284",
  "correlation_id": "trc_5d02...",
  "causation_id": "evt_c09f...",
  "baseline_id": "bl_2026_09_27_a",
  "at": "2026-09-27T21:14:03Z",
  "payload": { "job_id": "job_77e1", "model_ci": "qwen36@sha256:3f9a…", "output": { "...": "full validated output" } }
}
```

- `correlation_id` is the **trace ID**. It's set when an idea or story is created and inherited by every job, escalation, referral, commit, and gate that follows. The monitor's live feed and the audit log can then show the complete causal chain of any outcome, which was a gap noted in the tooling review.
- `causation_id` points to the event that directly caused this one.
- **Model outputs are stored in full as events.** This is the key to replay.

### 11.2 How it relates to the other stores

| Store | Answers | Written by |
|---|---|---|
| Engine event log (this doc) | "What is the operational state of work, and how did it get here?" | Engine only |
| `canonical_records` (data layer §3.4) | "What is currently true/decided on topic X?" — distributed to agents by need-to-know | Engine applying authorized proposals |
| `episodic_log` / `semantic_notes` (data layer §3.2–3.3) | "What does this agent know?" | Bus delivery / agent write-back |
| Audit log (escalation §8, governance §12) | "Prove what happened, tamper-evidently" | Audit writer, which consumes engine events + bus messages and hash-chains them |

### 11.3 Replay

Model outputs live in the log, so **replaying the log rebuilds any past state exactly without calling a model**. That supports:
- scrubbing back through a completed sprint for a retro (resolves the historical-replay question from monitor design v0.1; now monitor design §5.7)
- debugging ("show me the board at the moment this task was dispatched")
- verifying a projection bug fix against real history
- the "same prompt, same config" reproducibility claim in change/config design §3.4

---

## 12. Simulation mode

Because every side effect goes through a port, the engine can run the *whole company* against simulated adapters. This is where "business simulator" becomes literal. The company's behavior can be simulated for a fraction of a cent and a few seconds, instead of spending real tokens and wall-clock days to find out.

### 12.1 Stub agents

A simulated `ModelRouter` answers each job with a sampled outcome instead of a model call:

```yaml
# sim/behavior/developer.implement_task.yaml — calibrated from audit history (§12.3)
outcomes:
  complete:              0.71
  needs_clarification:   0.09     # → referral to PO
  retry_then_complete:   0.12
  retry_exhaustion:      0.08     # → tier escalation
duration_seconds: { dist: lognormal, median: 95, p90: 240 }    # at T0
tokens: { in: { median: 7800 }, out: { median: 1400 } }
tester_first_pass_rate: 0.64
```

There's one behavior model per `(role, job_type, tier)`. Before any history exists, the priors are rough guesses. After a few sprints they're fitted from real audit data.

### 12.2 Uses

| Use | Question it answers |
|---|---|
| **Engine & config testing** | Does this `workflow.yaml` / `org_chart.yaml` deadlock, starve a role, or overflow a WIP limit? Runs in CI for the AgentCo repo on every config change (change/config design §10). |
| **Capacity planning** | How many Developer sandboxes before the GPU semaphore saturates? Where's the bottleneck: Tester, Security Reviewer, T1? |
| **Delivery forecasting** | Monte Carlo over the remaining backlog gives **P50/P85 completion dates** per feature and epic. Client Comms reports ranges, never single dates. A committed date remains a `budget_or_timeline_commitment` that needs the human; the forecast is the evidence for that decision. |
| **Cost forecasting** | Expected T2/T3 spend per feature, and when the daily or epic budget guardrail will bind. |
| **What-if for change proposals** | "If Architect `min_confidence` goes 0.70 → 0.72, what happens to escalations and cost?" Fills the `impact` block of a change record with a simulated estimate (change/config design §5). |

### 12.3 Calibration

After each iteration, a calibration job (deterministic statistics, not an agent) refits behavior models from the audit log. It also reports **forecast error**: last PI's P85 against what actually happened. A forecast that's never checked against reality looks trustworthy without being so.

### 12.4 Limits

A simulation of the company is only as good as its behavior models. It predicts *flow* (throughput, queues, cost, dates) well and *content* (whether the code will be good) not at all. The monitor labels every simulated figure as simulated.

---

## 13. Economics

Every job posts cost entries to a **cost ledger**: tokens in and out, local GPU-seconds, wall-clock seconds, and dollars for T2/T3. Costs roll up task → story → feature → epic → portfolio.

- **Lean budget guardrails:** an epic or feature may carry `budget_usd`. At 80% the engine warns Exec Director; at 100% it stops scheduling *paid-tier* work on that item (local work continues) and raises a decision for Exec Dir, which may become a human request. This is where the escalation design's open `daily_spend_limit` question is implemented, one level down.
- **Unit economics** for the monitor's cost dashboard (monitor design §5.8): cost per story point, per feature, and per tier resolution; the dollar cost of each escalation path; the cost of `data_class_ceiling` skips.
- **Local isn't free.** GPU-seconds are tracked even though they cost nothing in dollars, because they're the throughput constraint. A T1 job that holds the GPU for 20 minutes has a real opportunity cost measured in blocked T0 work.

---

## 14. Failure handling and consistency

| Failure | Handling |
|---|---|
| Sandbox crash mid-job | Lease expires (heartbeat timeout) → job re-queued; attempt counted. Work state lives in the event log, not the sandbox (monitor design §4.2). |
| Job timeout | Per-tier timeout (`T0: 5m`, `T1: 30m`, `T2/T3: 5m` defaults). Counts as a failed attempt. |
| Duplicate delivery | Bus is at-least-once; every command carries an idempotency key (`job_id`, `commit_id`); appliers ignore repeats. |
| Concurrent edits | Work items carry `version`; a proposal based on a stale version is rejected and the job re-runs with fresh context. This is how stale knowledge (data layer §7.2) surfaces instead of silently overwriting. |
| Poison job | Fails at every tier → `dead_letter` + incident; parent item goes `blocked`. |
| Dependency cycle | Detected at task creation (graph check); the decomposition is rejected. |
| System-wide stall | All ready work blocked on the human or budget → engine enters `idle_blocked`, the monitor shows why, and one consolidated human notification goes out (not one per item). |
| Engine crash | Restart from the last snapshot + event log tail; in-flight leases expire and re-queue. The engine is a single writer, so there's nothing to reconcile. |

---

## 15. Walkthrough: idea to first merged task

1. The human types an idea into the monitor → `intake_gateway` → event `intake.received` (correlation `trc_5d02`).
2. Engine dispatches Client Comms (intake). It returns the captured idea with one flagged gap (guest checkout unspecified).
3. Engine dispatches Exec Dir (`set_vision`). Exec Dir judges the gap to be `ambiguous_original_intent` → `human_request` → Client Comms frames three options → monitor inbox.
4. Meanwhile, Exec Dir's vision for everything *not* affected by the gap is published. PO refines epics → features → stories. Stories touching checkout are parked `awaiting_human`; the rest proceed.
5. Architect (`decompose_story`, T0) splits `story_12` into three tasks. One exceeds `max_relevant_files`, so the engine rejects it with specifics. The re-split passes; tasks go `draft → ready`.
6. The scheduler leases `task_31` to `developer/sbx-14` (qwen36). The diff is returned; the driver verifies scope and commits to `task/task_31`. CI runs: green.
7. Security Reviewer passes; Tester fails one case → rework with failures attached → Developer fixes → Tester passes.
8. The change class is computed as **standard** → driver merges. `task_31 → done`. Cost ledger: 3 T0 jobs, 0 T1, $0.00, 11 GPU-minutes.
9. The human answers the inbox question. The decision record is published; parked checkout stories return to `ready` automatically.

Every step above is an event with `correlation_id: trc_5d02`. The monitor can show it as one continuous trace, and replay can rebuild any intermediate board.

---

## 16. Code layout

Consolidating the module paths scattered across the existing designs:

```
agentco/
  engine/                 # pure domain core — no I/O, no LLM
    work_items.py         # (was backlog/models.py)
    org.py                # org_chart.yaml model, job types, referral map
    workflow.py           # state machines + transition authority
    decomposition.py      # task_bounds + context-fit
    scheduler.py          # ready queue, WSJF, WIP, resources
    effects/              # one applier per role output type
    escalation.py         # trigger → tier escalation / referral (§9)
    ledger.py             # cost ledger + budget guardrails
    events.py, projections.py, replay.py
  ports/                  # interfaces the core depends on
  adapters/
    bus/                  # orchestration/message_bus.py + broker
    models/               # model_backends/router.py, llama_cpp, opencode_cli, claude_cli
    sandbox/  repo/  ci/  clock/  store/
    sim/                  # stub agents, virtual clock, behavior models (§12)
  driver/                 # process_supervisor, ceremony_scheduler, intake_gateway, human_relay
  monitor/                # FastAPI backend + static frontend
  compliance/             # audit_log writer, control tagging, chain verification
agentPrompts/  schemas/  evals/  baselines/
config/  org_chart.yaml  models.yaml  task_bounds.yaml  workflow.yaml  agile.yaml
         need_to_know.yaml  policy/
```

---

## 17. Build phases

The engine is buildable, and useful, before any model is connected:

| Phase | Delivers | Proves |
|---|---|---|
| **0 — Sim core** | Engine core, event store, workflow, scheduler, sim adapters, CLI to run a simulated backlog | State machines and config don't deadlock; the forecasting loop works end-to-end at zero cost |
| **1 — One real worker** | Real router (T0 only), one Developer + one Tester sandbox, driver commits, local CI on a toy repo | The job cycle, scope check, rework loop, and cost ledger on real output |
| **2 — The team** | PO (T2), Architect, Security Reviewer, broker ACLs, referrals | Decomposition under bounds; referral vs. tier escalation |
| **3 — The top** | Exec Dir and Client Comms (T3), human relay, monitor inbox and boards | The human loop, end to end |
| **4 — Governance** | Baselines, change records, eval gate, audit hash chain, compliance dashboard | The governance and change/config designs, enforced |
| **5 — Scale** | PI cadence, multi-instance roles, calibrated simulation forecasts | SAFe/PMO operation |

---

## 18. Changes required to existing documents

| Document | Change |
|---|---|
| Escalation design §3 | Tier set per **job type** (`org_chart.yaml`), not per role; `models.yaml` keeps model definitions and routing rules — **done in escalation design v0.2** |
| Escalation design §4.1, §7 | Worker contract gains `escalation.reason` / `question` / `blocking`; new triggers `rework_exhaustion` and `data_class_ceiling`; distinguish tier escalation from referral — **done in escalation design v0.2** |
| Escalation design §6.2 | Replace the token-count heuristic with the context-fit rule (§6.2 here) — **done in escalation design v0.2** |
| Data layer design §4 | Envelope gains `correlation_id` / `causation_id` — **done in data layer design v0.2** |
| Monitor design | Driver hosts the engine; ceremony scheduler becomes a clock adapter; historical replay is provided by the event log — **done in monitor design v0.2** (§4.1, §4.3, §5.7) |
| Prompt templates | Developer/Tester/Scrum Master/Architect/PO: "escalate to X" wording replaced with emitting `escalation.reason`; Architect gains a `decompose_story` template |

---

## 19. Open questions / follow-ups

- **Iteration length.** Is one day the right agent cadence, or should iterations close on committed scope alone, with no time box? The simulation mode can compare both once behavior models are calibrated.
- **Who decomposes when the Architect is busy.** Decomposition is on the critical path for every story. If it becomes a bottleneck even at T0, a separate `tech_lead` role, or multiple Architect instances for decomposition only, may be needed.
- **Engine and audit log merger.** The event log and audit log are both append-only with similar content. Merging them into one hash-chained store is simpler but mixes operational and compliance retention rules. Kept separate for v0.1.
- **Behavior-model fidelity.** Simulated outcomes are sampled independently per job; real failures are correlated (a bad story produces several failing tasks). A hierarchical model (per-story difficulty) is the likely next step.
- **Multiple products at once.** The work model assumes one portfolio. Running several client products concurrently needs per-product resource shares in the scheduler and per-tenant data classes (governance design §15).
