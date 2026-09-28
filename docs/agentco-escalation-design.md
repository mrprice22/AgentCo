# AgentCo — Tiered Escalation Architecture

**Design document v0.2**
**Scope:** How the LLM-staffed "company" routes work across available models, escalates issues upward by cost, and gates conversation with the human behind the single most expensive and most trusted agent (Claude Opus).

| Version | Changes |
|---|---|
| v0.1 | Original design: tiers, role defaults, escalation triggers and packet, human chokepoint, routing, worker contract, audit loop |
| v0.2 | Integrates the later designs: tiers set per job type (core engine), tier escalation vs. referral, data-class ceilings (governance), RACI-generated permissions and the engagement lock (agent API), the context-fit routing rule (fixes the long-input bug), degraded modes and circuit breakers (service management), Start/Pause, search-before-escalate (knowledge), and the owner-joined meeting exception. `project_memory` renamed to `canonical_records`. |

---

## 1. Goals

1. **Cheapest capable model does the work.** Free, local, fast models handle the volume of bounded tasks. Paid/remote models are invoked only when a cheaper tier genuinely cannot proceed.
2. **Escalation is a ladder, not a broadcast.** An agent that gets stuck asks the *next* tier up — never skips a rung (except the fast paths in §6.4), never asks two tiers at once, never asks the human directly. A question that belongs to *another role* isn't escalated at all — it's referred (§4.4).
3. **Only Claude Opus holds the conversation with the human.** Every other agent, at every tier, that believes "a human needs to decide this" is wrong by construction — its actual job is to escalate or refer. The chain terminates at Opus, and Opus alone decides whether the issue is *truly* a human-decision item or something it can resolve with its own authority. Two bounded exceptions exist, neither of which lets an agent start a conversation:
   - **Operational alerts** are templated notices from the driver (no LLM) about the state of the system, for when the conversation channel itself is down (service management design §1.3).
   - **Owner-joined meetings:** in the meeting types the owner chooses to attend (currently change approval), participants answer the owner directly and stay on the agenda (company directive D12, v1.2; change/config design §5.1).
4. **Cost, latency, data class, and run state are first-class routing inputs.** The router knows what each tier costs (dollars, tokens/sec, GPU contention), what data each tier may receive (§2.2), and whether the owner has the company running at all (§6.7). It picks the lowest tier that satisfies all of them.
5. **Every escalation is logged with a reason**, so the design can be tuned later (e.g., if `qwen36` escalates 40% of testing tasks, the task bounds or prompt for that role are wrong, not the model).

---

## 2. Inventory: what we're actually routing across

### 2.1 Host

| | |
|---|---|
| Machine | Windows box — Ryzen 7 9700X, 61.7 GB RAM, RTX 3060 8 GB |
| Local endpoint | `http://10.42.0.83:8080/v1` (OpenAI-compatible, **no auth**, open on `10.42.0.0/24`) |

Because auth is off and the link is a flat LAN, treat the endpoint as **trusted-network-only**: the router process must run on that LAN (or a host with a route to it), and nothing in this design should expose `10.42.0.83:8080` beyond that subnet. This has an architectural consequence below (§6.5).

The RTX 3060's 8 GB VRAM is the real constraint: it cannot hold all three local models resident at full GPU offload simultaneously. Assume the llama.cpp/OpenAI-compatible server is doing partial CPU/GPU split or swap-on-demand — which means **concurrent requests to different local `model` values contend for the same GPU**, and switching models costs a reload. The router must serialize or explicitly queue local-tier requests (§6.3).

### 2.2 Model tiers

| Tier | `model` / access | Cost | Speed | Context | Data-class ceiling | Role in the org |
|---|---|---|---|---|---|---|
| **T0 — Worker (fast)** | `qwen36` (Qwen3.6-35B-A3B, Q4_K_M, MoE 3B active) | Free (local) | ~42 tok/s gen, ~510 tok/s prompt | 32K | Confidential | Default for most bounded work |
| **T0 — Worker (fast prompt)** | `deepseek9b` (Qwen3.5-9B distilled from DeepSeek-V4-Flash) | Free (local) | ~40 tok/s gen, ~1630 tok/s prompt | 16K | Confidential | Same rung as `qwen36`; picked when the prompt fits its smaller window and time-to-first-token matters (§6.2) |
| **T1 — Local hard-question** | `deepseek` (DeepSeek-V4-Flash-0731, UD-IQ1_S) | Free (local, but expensive in *time*) | ~4 tok/s gen (warm), ~2 tok/s cold | 16K | Confidential | The "think harder before we spend real money" rung — one hard question, then walk away |
| **T2 — Remote mid-tier** | DeepSeek-V4-pro via OpenCode | Paid (API) | Fast (remote infra) | Large | **Internal** (until the supplier assessment raises it) | Product and cross-cutting technical reasoning |
| **T3 — Remote top-tier (terminal)** | Claude Opus via `claude` CLI | Most expensive | Fast (remote infra) | Large | Confidential | Executive Director and Client Communications. **Holds the conversation with the human.** |
| **T4 — Human** | The owner | N/A (their attention is the scarcest resource in the system) | N/A | N/A | — | Client/customer. Reached via T3, plus the two bounded exceptions in §1. |

Ceilings come from governance design §5.2. A prompt's class is the highest class of anything assembled into it; no prompt goes to a tier whose ceiling is lower (§6.1).

Note the asymmetry that makes this design non-obvious: **T1 is free but slow**, and **T2/T3 are paid but fast**. A naive "escalate by cost" ladder would skip straight from T0 to T2 whenever a task fails, because T1 is a bad user-experience (multi-minute wall clock for one question). The design deliberately still routes through T1 first (§4), because:

- T1 costs $0 regardless of how often it's invoked, which matters over a long autonomous run.
- Many "T0 gave up" cases are actually solvable by a slower, more careful *local* pass — not by more capability, just more inference-time compute (matches DeepSeek-V4-Flash's own design intent).
- It caps how often the paid tiers get invoked, which is the actual cost lever we care about.

Two things override that ordering:
- **Latency-sensitive and security checkpoints** (§6.4) skip T1 and go straight up, because waiting on a 4 tok/s model to fail first is the worse trade.
- **Data class:** while T2's ceiling is Internal, a Confidential task that fails at T1 skips T2 and goes to T3 (`data_class_ceiling`, §4.1). That's more expensive by design, and the cost view shows it.

---

## 3. Role and job type → tier assignment

Tiers are set **per job type**, not per role, in `org_chart.yaml` (core engine design §3). One role does jobs of very different difficulty: an Architect's story decomposition happens once per story and is fairly mechanical, while its cross-cutting decisions are rare and hard. `models.yaml` only defines the models themselves (§3.1).

| Role | Job type(s) → default tier | Rationale |
|---|---|---|
| Developer | `implement_task` → T0 | Bounded, single-file/single-function tasks — exactly what T0 is sized for |
| Tester | `test_task` → T0 | Testing against a spec is bounded |
| Security Reviewer (NIST SSDF gate) | `security_gate` → T0, fast path to T2 | Checklist review against scanner output starts at T0; ambiguous findings escalate immediately (a false negative is expensive) |
| Scrum Master | ceremonies, `maintain_dashboard` → T0 (T1 for process conflicts) | Status aggregation and bookkeeping — mechanical |
| Architect | `decompose_story` → **T0**; `decide`, `review_design` → **T1** | Decomposition is on every story's critical path, so it has to be cheap; hard technical decisions are exactly the "one hard question" T1 is for |
| Product Owner | `refine_backlog`, `answer_referral` → **T2** | Prioritization and story-writing benefit from stronger reasoning; PO decisions ripple into many tasks |
| Change Coordinator | CR checks, release assembly → T0; release notes and impact summaries → T2 | Prepares change paperwork; never approves (agent API design §5.3) |
| Executive Director | decisions → **T3 (Opus)** | Vision, cross-epic tradeoffs, and the call on whether the human is needed |
| Client Communications | intake, framing, reports, `converse` → **T3 (Opus)** | Everything the human reads comes from this role, so it must be the most reliable voice in the system (§5) |

```yaml
# org_chart.yaml (excerpt) — ladders live on job types
roles:
  developer:
    job_types:
      implement_task:  { default_tier: T0, ladder: [T0, T1, T2, T3] }
  architect:
    job_types:
      decompose_story: { default_tier: T0, ladder: [T0, T1, T2] }
      decide:          { default_tier: T1, ladder: [T1, T2, T3] }
  product_owner:
    job_types:
      refine_backlog:  { default_tier: T2, ladder: [T2, T3] }
  executive_director:
    job_types:
      decide:          { default_tier: T3, ladder: [T3] }   # nowhere left but the human
  client_communications:
    job_types:
      intake:          { default_tier: T3, ladder: [T3] }
      converse:        { default_tier: T3, ladder: [T3] }
```

### 3.1 `models.yaml`

```yaml
models:
  qwen36:          { tier: T0, endpoint: local, context: 32768, ceiling: confidential, usd_per_mtok: 0 }
  deepseek9b:      { tier: T0, endpoint: local, context: 16384, ceiling: confidential, usd_per_mtok: 0 }
  deepseek:        { tier: T1, endpoint: local, context: 16384, ceiling: confidential, usd_per_mtok: 0 }
  deepseek-v4-pro: { tier: T2, endpoint: opencode, ceiling: internal }
  claude-opus:     { tier: T3, endpoint: claude_cli, ceiling: confidential }
```

The router's allow-list *is* this file together with the model inventory (governance design §8.1): a model that isn't listed can't be routed to.

### 3.2 Who may do what

Capabilities are **not** configured per role in this file. They're generated from the RACI matrix into `permissions.lock` (agent API design §4). In particular, the `human_relay` verb exists only for the Executive Director and Client Communications, which is what makes "only Opus holds the conversation" enforceable in code. Confidence thresholds (`min_confidence`) come from `risk_tolerance.yaml` (governance design §4): 0.60 by default, 0.65 for the PO, 0.70 for the Architect, and 0.75 for the Security Reviewer.

---

## 4. The escalation protocol

### 4.1 Why an agent escalates

An agent escalates when it hits one of a small, enumerated set of conditions — not a vague "I'm unsure." Vague uncertainty is exactly what smaller models fake convincingly, so escalation must be triggered by **structural** signals the harness can detect, not by the model's self-report alone (though self-report is one input).

| Trigger | Detected by | Action (§4.4) |
|---|---|---|
| **Retry exhaustion** | Worker failed its own output-schema/test validation N times (`max_local_retries`, default 2) | Tier escalation |
| **Declared ambiguity** | Structured output has `status: needs_clarification` with an `escalation.reason` (§7) | Referral to the owning role |
| **Scope violation** | The **driver's** check of `files_touched` against `relevant_files` at commit time, or the worker's own report (change/config design §8.2) | Referral to the Architect (re-decompose) |
| **Rework exhaustion** | Tester fail → rework cycles reached `rework_limit` (default 3) | Referral to the Architect (core engine design §5.2) |
| **Confidence below threshold** | Self-scored confidence below the role's `min_confidence` (§3.2) | Tier escalation |
| **Policy/security flag** | Security Reviewer can't clear a finding at its tier | Tier escalation, fast path (§6.4) |
| **Explicit "decision" content** | Output classified as a business/product/strategic decision (keyword + schema check: pricing, legal, irreversible external actions, or contradicting an existing decision in `canonical_records`) | Referral (PO or Exec Dir) |
| **Data-class ceiling** | The next tier's ceiling is below the prompt's class (§2.2) | Tier escalation to the next eligible tier |
| **Tier unavailable** | Circuit breaker open for that tier's supplier (§6.6) | Per degraded mode (§6.6) |
| **Capacity overflow** | Local queue past `local_queue_overflow` (§6.3) | Route new T0 work up, logged separately |

### 4.2 Why escalation stops being "escalation" at T3

At T0/T1/T2, escalating means: *package the problem and hand it to a more capable/expensive agent, who will very likely just solve it.* At T3 (Opus), there's no more capable agent to hand it to — so the "escalation" trigger set is re-interpreted as a **decision**, not a hand-off:

- Opus receives the same escalation packet any lower tier would produce.
- Opus's job is to determine: *is this actually resolvable with the authority the Executive Director already has* (i.e., can Opus just decide it), *or does it require information/preference only the human has* (budget, taste, business priorities not yet stated, legal/compliance risk acceptance, irreversible external commitments)?
- Only in the second case does Opus invoke `human_relay`.

This is the crux of the design: **the ladder doesn't have a rung above Opus, so Opus's escalation trigger set is narrower and different in kind** — it's specifically "things no LLM should decide unilaterally," not "things this model finds hard."

The set is the `always_human` list in `risk_tolerance.yaml` (governance design §4), which the owner controls:

```yaml
always_human:
  - irreversible_external_action       # e.g. sending an email, spending money, deleting prod data
  - budget_or_timeline_commitment      # anything that promises the human a date or cost
  - legal_or_compliance_risk_acceptance
  - ambiguous_original_intent          # genuinely underspecified, not just under-decomposed
  - explicit_human_request_to_be_asked # the human said "check with me before X"
  - production_deployment              # unless the owner's pre-approval catalog covers it (change/config §4.3)
  - new_remote_supplier_or_data_class_exception
  - governance_config_change
# conflicting_stakeholder_input stays reserved for multi-stakeholder mode (§9)
```

Anything else — including things a human might *want* to weigh in on but that Opus can reasonably decide and flag in the decision log — Opus resolves itself and logs the rationale (§8). The owner reviews those decisions every iteration in the console's *resolved without you* view (monitor design §5.6). This keeps the human's attention reserved for the small number of things that actually need it.

### 4.3 The escalation packet (data contract between tiers)

Every escalation, at every rung, uses the same envelope so the receiving tier doesn't need tier-specific parsing:

```json
{
  "escalation_id": "esc_9f2a...",
  "correlation_id": "trc_5d02...",
  "origin_role": "developer",
  "origin_tier": "T0",
  "origin_model": "qwen36",
  "engagement_id": "eng-dev-sbx14-it42",
  "task_id": "task_1284",
  "trigger": "retry_exhaustion",
  "escalation": { "reason": "low_confidence", "question": "...", "blocking": true },
  "kb_checked": ["kb-0142"],
  "data_class": "internal",
  "attempts": [
    {"model": "qwen36", "output": "...", "validation_errors": ["..."]},
    {"model": "qwen36", "output": "...", "validation_errors": ["..."]}
  ],
  "task_context": {
    "story_id": "story_88",
    "acceptance_criteria": "...",
    "relevant_files": ["src/foo.py"],
    "constraints": "..."
  },
  "requested_action": "unblock_or_resolve",
  "confidence": 0.4,
  "escalation_chain": ["T0"],
  "referral_chain": []
}
```

Each tier that touches it *appends* to `escalation_chain` and either resolves it (writes a `resolution` field and sends it back down) or re-escalates (adds its own attempt + trigger and passes it up unchanged in shape). Referrals append to `referral_chain` instead. This makes the full path auditable after the fact regardless of how far it went.

To keep packets inside the smaller local windows (§6.2), only the most recent attempt's output travels in full. Earlier attempts carry just their validation errors; the full text stays in the audit log.

Everything in `attempts[].output` and `escalation.question` is **untrusted content** when it reaches a higher tier. Text written by a lower tier can't carry authority upward (governance design §7; company directive D7).

### 4.4 Tier escalation vs. referral

The two are different operations, and the engine, not the agent, decides which applies (core engine design §9):

- **Tier escalation:** the *same job*, retried by a stronger model on the job type's ladder. The role doesn't change. Triggers: retry exhaustion, low confidence, security flags, data-class ceilings.
- **Referral:** the question *isn't this role's to answer*. It goes to the role that owns it, at that role's own default tier. Examples: ambiguous acceptance criteria (→ Product Owner), scope violation or rework exhaustion (→ Architect), business tradeoffs (→ Product Owner), beyond PO authority (→ Executive Director).

Escalating an ambiguous acceptance criterion up the *tier* ladder would just pay a smarter model to guess at the Product Owner's intent. It needs a referral. Agents state *what kind* of help they need through `escalation.reason`, an enum per role (§7). The engine maps `(role, reason)` to an action; agents never choose the destination. Referral loops are guarded: at most four hops, and a question returning to a role it already visited goes to the Executive Director with the full chain.

### 4.5 Search before escalating

Before an agent may call `escalate` or `refer`, the Agent API requires a knowledge-base search on the issue text (knowledge design §5.1). The escalation must carry `kb_checked`: the article IDs considered, or `none_applicable` with a reason when a strong match was returned. An escalation that an existing article resolves never happens (a **deflection**). Escalations after a search found nothing are flagged as **article gaps**, which become knowledge work. This is also a standing obligation in every engagement contract (agent API design §6.1).

---

## 5. Human-in-the-loop chokepoint

```
        ┌─────────────────────────────────────────────┐
        │                    HUMAN                     │
        └───────────────────────▲───────────────────────┘
                                 │  conversation: only via human_relay
                                 │  (+ operational alerts from the driver,
                                 │     + owner-joined meetings — §1)
        ┌───────────────────────┴───────────────────────┐
        │      T3 — Claude Opus (Exec Dir / Client Comms) │
        │      re-classifies: "decide myself" vs "ask"    │
        └───────────────────────▲───────────────────────┘
                                 │ escalate
        ┌───────────────────────┴───────────────────────┐
        │      T2 — DeepSeek-V4-pro (via OpenCode)        │
        │      Product Owner / cross-cutting reasoning    │
        └───────────────────────▲───────────────────────┘
                                 │ escalate
        ┌───────────────────────┴───────────────────────┐
        │      T1 — local `deepseek` (slow, free)         │
        │      "one hard question" rung                   │
        └───────────────────────▲───────────────────────┘
                                 │ escalate
        ┌───────────────────────┴───────────────────────┐
        │  T0 — local `qwen36` / `deepseek9b` (fast, free)│
        │  Developer / Tester / Scrum Master / bounded work│
        └─────────────────────────────────────────────────┘
```

**Two channels, never mixed.** *Conversation* is anything an agent wants the human to know or decide. It flows only through `human_relay` → Client Communications → the console inbox. *Operational alerts* are the driver's templated notices about system state (service mode, incidents, stalled delivery), sent even when T3 is down. They contain no agent-authored content (service management design §1.3). The one conversational exception is a meeting the owner has chosen to join (§1). Even there, agents can't raise new topics or ask for decisions outside the agenda, and decisions are made with console buttons, never text (change/config design §5.1).

`human_relay` is a verb only T3 roles hold, enforced two ways (defense in depth):

1. **Permission generation:** the `human_relay` verb is generated into `permissions.lock` only for the Executive Director and Client Communications, from the RACI matrix (agent API design §4). T0–T2 sandboxes have no such verb; a call is denied at the gateway before anything else happens.
2. **Runtime model check:** `human_relay` independently checks that the calling job ran on an allow-listed model (`claude-opus`) before it will deliver anything to the human-facing channel. A misconfigured permission alone can't grant the capability.

When invoked, `human_relay` doesn't just dump the raw escalation packet on the human — it hands it to Client Communications (also T3) to render as a schema-validated human request: a plain-language question, a few options, a recommendation, a deadline, the default if unanswered, and the cost of waiting (service management design §3.2):

> "The original idea didn't specify whether guest checkout should be allowed. This affects 3 stories already in progress. Which do you want: (a) require accounts, (b) allow guest checkout with email-only, (c) allow guest checkout with no data collection at all? Recommended: (b)."

The human's answer re-enters the system as a **decision record** in `canonical_records`, tagged with the `escalation_id` and distributed through the normal broker path, so it's available to every future task without re-asking (§8; monitor design §6).

---

## 6. Routing mechanics

### 6.1 Router responsibilities

`model_backends/router.py` is the traffic cop. Agents reach it only through the Agent API's `model.complete` verb (agent API design §2.2). It has six jobs:

1. **Pre-dispatch checks, in order:**
   1. run state (§6.7)
   2. a bound, locked, active engagement for the calling sandbox (no policy, no inference; agent API design §6.4)
   3. the data-class ceiling of the candidate tier (§2.2)
   4. budget: product and engagement guardrails (core engine design §13)
   5. circuit breaker state (§6.6)
2. Resolve `job type → default tier` from `org_chart.yaml`, then pick the model within the tier by the context-fit rule (§6.2).
3. On tier escalation, resolve `current tier → next eligible tier` from the job type's ladder, skipping tiers ruled out by data class or breakers.
4. Enforce local-GPU concurrency limits and model affinity (§6.3).
5. Record provider-reported model IDs for remote drift detection (change/config design §3.3), and run the egress secret scan on every outbound T2/T3 prompt (governance design §5.3).
6. Track and expose cost/latency metrics per tier, so routing decisions and budget alarms use real data instead of the static table in §2.2.

### 6.2 Model selection within a tier: the context-fit rule

v0.1 sent inputs over 10K tokens to `deepseek9b` for its prompt speed. Since `deepseek9b` has the *smaller* window (16K vs. 32K), a long enough input overflowed it. v0.2 selects by fit first:

```python
def select_model(tier, job, loaded_model):
    need = job.prompt_tokens + job.max_output_tokens + CONTEXT_MARGIN
    candidates = [m for m in MODELS.at_tier(tier)
                  if m.context >= need and m.ceiling >= job.data_class]
    if not candidates:
        # Task bounds should have prevented this (core engine §6.2):
        # treat it as a scope problem, not a capability one.
        raise Referral(reason="scope_violation", to="architect")
    if loaded_model in candidates:              # avoid a reload (model affinity)
        return loaded_model
    if job.sla_seconds is not None and job.sla_seconds < 30:
        return max(candidates, key=lambda m: m.prompt_tok_per_s)   # fastest TTFT
    return DEFAULT_FOR_TIER[tier] if DEFAULT_FOR_TIER[tier] in candidates else candidates[0]
```

**Fit is checked when config loads, per ladder.** For every tier on a job type's ladder, at least one model at that tier must fit the job's bounds plus prompt-template overhead plus the escalation packet it would receive (§4.3). T1's 16K window is the binding limit for ladders that include it, which is why core engine design §6.2's example bounds are a 10K prompt and a 3K output. The engine refuses to start with a bound that can't fit. Within those bounds every T0 task fits `qwen36`; `deepseek9b` is chosen only when the prompt also fits its window and time-to-first-token matters.

### 6.3 Local GPU contention

Since T0 (two model names) and T1 (one model name) all compete for the same 8 GB card:

- The router maintains a **single local-resource semaphore** shared across `qwen36`, `deepseek9b`, and `deepseek`. Its concurrency is set from the benchmark harness (service management design §7.2), not guessed. **CI jobs take slots in the same scheduler**, because builds and scans compete for the CPU that llama.cpp's offloaded layers use (change/config design §8.3).
- **Model affinity:** the scheduler prefers jobs for the currently loaded model within a bounded fairness window (default 5 minutes), because every switch risks a weight reload (service management design §7.3).
- T1 requests are rare and slow by nature (4 tok/s). They get **lower scheduling priority** than T0 requests, since blocking the whole developer/tester pipeline on one architect's hard question is the wrong trade. In practice: queue T1 requests and dispatch them only when no T0 tasks are pending, unless a T0 pipeline is blocked waiting on that specific T1 answer, in which case it's promoted.
- If the local queue depth exceeds `local_queue_overflow`, the router may route *new* T0 work directly to T2 instead of queuing, trading a small dollar cost for throughput. That's only allowed where the data class fits T2's ceiling and budget remains. It's a deliberate escape valve, logged distinctly (`trigger: "capacity_overflow"`, not a capability failure) so it doesn't pollute the tuning signal from goal 5 (§1).

### 6.4 Skip-T1 fast path

As noted in §2.2, certain trigger types bypass T1 and escalate directly:

- `security_flag` (NIST SSDF gate failures) goes T0 → T2, because correctness matters more than cost here. If the data class exceeds T2's ceiling, it goes to T3.
- Anything already tagged `always_human`-adjacent by a keyword/schema pre-check at T0 (e.g., the worker's own output mentions pricing, deletion, or an external send). There's no point burning a slow T1 pass on something that's going to end up at T3 anyway.

### 6.5 Network/auth note

Because `10.42.0.83:8080` has no auth and is only reachable on the `10.42.0.0/24` LAN, **the router process holds the only reference to that base URL.** Sandboxes never reach it; their only network destination is the Agent API on loopback (agent API design §2.1), and they reach models only through `model.complete`. T2/T3 calls go out over the internet (OpenCode, `claude` CLI) from the same host using credentials only the driver holds (governance design §6.4), so no additional exposure is introduced. If this harness is ever run from a machine off that subnet, T0/T1 become unreachable and the router should treat that as **all local tiers down** — not retry forever — and fall back per §6.6.

### 6.6 Tier unavailable: breakers and degraded modes

Each supplier endpoint (local server, OpenCode/DeepSeek, Anthropic) has a **circuit breaker**. It opens after 5 consecutive failures or ≥ 50% failures over 2 minutes, stops calls while open, and probes with one half-open call on a backoff up to 10 minutes (service management design §4.4). An open breaker puts the engine in a degraded mode (service management design §4.2):

- **Local down:** T0/T1 work routes to T2 **only if** the data class fits T2's ceiling and budget remains. Otherwise it's parked. Logged `trigger: "tier_unavailable"`.
- **T2 down:** only *blocking* T2 work (referral answers blocking other work) goes to T3, within budget; the rest is parked.
- **T3 down:** T3 jobs queue. **Opus's authority never fails over to a weaker tier.** No other model acts as Executive Director or talks to the human. The driver sends an operational alert and everything that doesn't need T3 continues (service management design §4.3).

Rate-limit responses (HTTP 429) aren't outages. They're handled by honoring retry-after and reducing concurrency to that supplier, and tracked separately so a busy supplier doesn't look like a failed one.

### 6.7 Paused

The owner's **Start / Pause** control (monitor design §4.2) is the first check on every dispatch. While paused, no job is leased and `model.complete` is refused for every sandbox, chat included, so the company spends nothing. That's the owner's cost control, used instead of a chat budget. Breakers and budgets still apply while running; Pause simply means nothing runs.

---

## 7. Worker contract

Every dispatch, at every tier, returns a strict schema (per role, under `schemas/<role>.output.json`) so escalation triggers (§4.1) can be detected mechanically rather than by re-reading prose output:

```json
{
  "status": "complete | needs_clarification | blocked",
  "output": { "...task-specific..." },
  "escalation": { "reason": "<enum for this role>", "question": "...", "blocking": true },
  "kb_checked": ["<article ids>"],
  "confidence": 0.0,
  "validation": { "self_check_passed": true, "notes": "" },
  "files_touched": ["..."],
  "reasoning_summary": "one paragraph, for audit log only"
}
```

- `escalation` is required whenever `status` isn't `complete`. `reason` is a closed enum per role, defined in `org_chart.yaml` (core engine design §8.2). A free-text reason is a schema failure.
- `kb_checked` is required whenever `escalation` is present (§4.5).
- `status != "complete"` or `confidence < min_confidence` triggers the engine's escalation/referral logic automatically; the dispatcher never needs a separate "did this actually work" model call.
- Free-text fields (`reasoning_summary`, `question`, and task-specific explanations) are never parsed for commands (governance design §7).

---

## 8. Audit & tuning loop

Every escalation packet (§4.3), every referral, every `human_relay` invocation, and every "Opus decided not to ask" resolution is appended to the audit log (governance design §12). Each entry carries:

- `correlation_id`, `baseline_id`, and the model and prompt configuration items used (change/config design §3.4), so any decision can be traced to the exact prompt text, model hash, and config it ran under.
- `controls`: the NIST SSDF / AI RMF / 800-53 controls the event evidences.
- Hash-chain fields (`prev_hash`, `entry_hash`) for tamper evidence.
- The full escalation chain and tier-by-tier attempts, with cost and tokens per tier, so the real $ cost per resolved issue is computable.
- The final resolution and which tier or role produced it.
- For `human_relay` calls: the rendered question, the human's raw answer, and the decision record written to `canonical_records`.

This log is the feedback source for two ongoing tuning activities:
1. **Task-bound tuning:** job types with high T0→T1/T2 escalation rates indicate `task_bounds.yaml` is splitting too coarsely for that task type.
2. **Ladder tuning:** if T1 (`deepseek`, 4 tok/s) resolves less than some threshold (e.g., 20%) of what it receives before further escalation, it's not earning its wall-clock cost and that job type's ladder should skip straight T0→T2.

**The tuning loop proposes; it never applies.** Its findings become system change proposals with the supporting data attached, routed to the Executive Director and on to the owner for approval (change/config design §6). The loop never edits `org_chart.yaml`, `models.yaml`, or `task_bounds.yaml` itself.

---

## 9. Open questions / follow-ups

- **Concurrency ceiling for local models:** still unmeasured. It's now a roadmap task (`story-bench-concurrency-ceiling`), set from the benchmark harness rather than guessed.
- **Cost ceiling / budget alarm:** *resolved.* Budgets live in `risk_tolerance.yaml` (governance design §4) and the cost ledger's guardrails (core engine design §13), and the owner's Start/Pause control stops all spend (§6.7).
- **Multi-human / stakeholder mode** is out of scope for v0.2 (single-client assumption throughout). The packet schema stays generic enough to extend, and `conflicting_stakeholder_input` is reserved. Tracked as `epic-multi-client`.
- **T2's ceiling:** until the DeepSeek/OpenCode supplier assessment is done (`dec-t2-ceiling`), Confidential work skips T2. Measure what that costs.
