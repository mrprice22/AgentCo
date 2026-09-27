# AgentCo — Tiered Escalation Architecture

**Design document v0.1**
**Scope:** How the LLM-staffed "company" routes work across available models, escalates issues upward by cost, and gates human interaction behind the single most expensive/most trusted agent (Claude Opus).

---

## 1. Goals

1. **Cheapest capable model does the work.** Free, local, fast models handle the volume of bounded tasks. Paid/remote models are invoked only when a cheaper tier genuinely cannot proceed.
2. **Escalation is a ladder, not a broadcast.** An agent that gets stuck asks the *next* tier up — never skips a rung, never asks two tiers at once, never asks the human directly.
3. **Only Claude Opus talks to the human.** Every other agent, at every tier, that believes "a human needs to decide this" is wrong by construction — its actual job is to escalate to the tier above it. The chain terminates at Opus, and Opus alone decides whether the issue is *truly* a human-decision item or something it can resolve with its own authority/judgment.
4. **Cost and latency are first-class routing inputs**, not an afterthought — the router knows what each tier costs (in dollars, tokens/sec, and GPU contention) and picks the lowest tier that satisfies the task's declared bound.
5. **Every escalation is logged with a reason**, so the design can be tuned later (e.g., if `qwen36` escalates 40% of testing tasks, the task bounds or prompt for that role are wrong, not the model).

---

## 2. Inventory: what we're actually routing across

### 2.1 Host

| | |
|---|---|
| Machine | Windows box — Ryzen 7 9700X, 61.7 GB RAM, RTX 3060 8 GB |
| Local endpoint | `http://10.42.0.83:8080/v1` (OpenAI-compatible, **no auth**, open on `10.42.0.0/24`) |

Because auth is off and the link is a flat LAN, treat the endpoint as **trusted-network-only**: the router process must run on that LAN (or a host with a route to it), and nothing in this design should expose `10.42.0.83:8080` beyond that subnet. This has an architectural consequence below (§7.4).

The RTX 3060's 8 GB VRAM is the real constraint: it cannot hold all three local models resident at full GPU offload simultaneously. Assume the llama.cpp/OpenAI-compatible server is doing partial CPU/GPU split or swap-on-demand — which means **concurrent requests to different local `model` values contend for the same GPU**, not just for compute. The router must serialize or explicitly queue local-tier requests (§6.3).

### 2.2 Model tiers

| Tier | `model` / access | Cost | Speed | Context | Role in the org |
|---|---|---|---|---|---|
| **T0 — Worker (fast)** | `qwen36` (Qwen3.6-35B-A3B, Q4_K_M, MoE 3B active) | Free (local) | ~42 tok/s gen, ~510 tok/s prompt | 32K | Default developer/tester/most-tickets model |
| **T0 — Worker (long-context / fast turnaround)** | `deepseek9b` (Qwen3.5-9B distilled from DeepSeek-V4-Flash) | Free (local) | ~40 tok/s gen, ~1630 tok/s prompt | 16K | Same rung as `qwen36`; picked instead when input is long or SLA is tight |
| **T1 — Local hard-question** | `deepseek` (DeepSeek-V4-Flash-0731, UD-IQ1_S) | Free (local, but expensive in *time*) | ~4 tok/s gen (warm), ~2 tok/s cold | 16K | The "think harder before we spend real money" rung — one hard question, then walk away |
| **T2 — Remote mid-tier** | DeepSeek-V4-pro via OpenCode | Paid (API) | Fast (remote infra) | Large | Architectural/story-level reasoning, cross-cutting technical judgment calls |
| **T3 — Remote top-tier (terminal)** | Claude Opus via `claude` CLI | Most expensive | Fast (remote infra) | Large | Executive Director / Product Owner authority. **Only agent allowed to contact the human.** |
| **T4 — Human** | The person | N/A (their attention is the scarcest resource in the system) | N/A | N/A | Client/customer. Reached only via T3. |

Note the asymmetry that makes this design non-obvious: **T1 is free but slow**, and **T2/T3 are paid but fast**. A naive "escalate by cost" ladder would skip straight from T0 to T2 whenever a task fails, because T1 is a bad user-experience (multi-minute wall clock for one question). The design deliberately still routes through T1 first (§4), because:

- T1 costs $0 regardless of how often it's invoked, which matters over a long autonomous run.
- Many "T0 gave up" cases are actually solvable by a slower, more careful *local* pass — not by more capability, just more inference-time compute (matches DeepSeek-V4-Flash's own design intent).
- It caps how often the paid tiers get invoked, which is the actual cost lever we care about.

The one place this is overridden is **latency-sensitive human-facing checkpoints** (§6.4) — those skip T1 and go straight to T2/T3, because making the client wait on a 4 tok/s model to fail before escalating is a worse trade than the token cost of asking DeepSeek-V4-pro directly.

---

## 3. Role → tier default assignment

This extends the `org_chart.yaml` / `models.yaml` split from the repo design. Each role has a **default tier** (where its normal work executes) and inherits the escalation ladder starting from that tier.

| Role | Default tier | Rationale |
|---|---|---|
| Developer | T0 (`qwen36`) | Bounded, single-file/single-function tasks — exactly what T0 is sized for |
| Tester | T0 (`qwen36` or `deepseek9b`) | Generating/running tests against a spec is bounded; `deepseek9b` when the code-under-test is long |
| Security Reviewer (NIST SSDF gate) | T0 → escalates fast | Checklist-style review starts at T0, but any ambiguous finding escalates immediately (a security false-negative is expensive) |
| Scrum Master | T0/T1 | Ceremony bookkeeping, status aggregation — mechanical, rarely needs escalation |
| Architect | **T1 default**, escalates to T2 readily | Cross-task technical decisions are exactly the "one hard question" T1 is for; if T1's answer isn't load-bearing enough, this is a T2 call |
| Product Owner | **T2 default** | Backlog prioritization and story-writing benefit from stronger reasoning than local models offer; PO decisions ripple into many tasks, so the cost is justified |
| Executive Director | **T3 (Opus)** | Vision-setting, cross-epic tradeoffs, and — critically — sole authority to escalate to the human |
| Client Communications | **T3 (Opus)** | Drafts anything the human sees; must be the most reliable voice in the system, and is structurally the same agent that owns the human-escalation decision (see §5) |

`models.yaml` gets a new required field per role:

```yaml
roles:
  developer:
    default_tier: T0
    default_model: qwen36
    fallback_model: deepseek9b     # used when input > ~12K tokens
    escalation_ladder: [T0, T1, T2, T3]
  architect:
    default_tier: T1
    default_model: deepseek
    escalation_ladder: [T1, T2, T3]
  product_owner:
    default_tier: T2
    default_model: deepseek-v4-pro
    escalation_ladder: [T2, T3]
  executive_director:
    default_tier: T3
    default_model: claude-opus
    escalation_ladder: [T3]         # nowhere left to escalate but the human
    can_contact_human: true
  client_communications:
    default_tier: T3
    default_model: claude-opus
    can_contact_human: true
```

Only roles with `can_contact_human: true` may invoke the human-relay tool. In practice that's Executive Director and Client Communications — both running on Opus — which is what makes "only Opus reaches the human" enforceable in code, not just convention.

---

## 4. The escalation protocol

### 4.1 Why an agent escalates

An agent escalates when it hits one of a small, enumerated set of conditions — not a vague "I'm unsure." Vague uncertainty is exactly what smaller models fake convincingly, so escalation must be triggered by **structural** signals the harness can detect, not by the model's self-report alone (though self-report is one input).

| Trigger | Detected by |
|---|---|
| **Retry exhaustion** | Worker failed its own output-schema/test validation N times (config: `max_local_retries`, default 2) |
| **Declared ambiguity** | Model's structured output includes `"status": "needs_clarification"` (part of the worker contract, §4.3) |
| **Scope violation** | Task, once attempted, clearly requires touching more files/modules than `task_bounds.yaml` allows — the split was wrong, not the worker |
| **Confidence below threshold** | Model asked to self-score confidence 0–1 as part of output schema; below `min_confidence` (default 0.6) routes up |
| **Policy/security flag** | Security Reviewer role finds anything matching a NIST SSDF gate it can't clear at its tier |
| **Explicit "decision" content** | Output classified as a business/product/strategic decision rather than an execution detail (keyword + schema check, e.g. touches pricing, legal, irreversible external actions, or contradicts an existing PO decision in `project_memory`) |

### 4.2 Why escalation stops being "escalation" at T3

At T0/T1/T2, escalating means: *package the problem and hand it to a more capable/expensive agent, who will very likely just solve it.* At T3 (Opus), there's no more capable agent to hand it to — so the "escalation" trigger set is re-interpreted as a **decision**, not a hand-off:

- Opus receives the same escalation packet any lower tier would produce.
- Opus's job is to determine: *is this actually resolvable with the authority a Product Owner/Executive Director already has* (i.e., can Opus just decide it), *or does it require information/preference only the human has* (budget, taste, business priorities not yet stated, legal/compliance risk acceptance, irreversible external commitments)?
- Only in the second case does Opus invoke the human-relay tool.

This is the crux of the design: **the ladder doesn't have a rung above Opus, so Opus's escalation trigger set is narrower and different in kind** — it's specifically "things no LLM should decide unilaterally," not "things this model finds hard."

Concretely, `client_communications.py` / `executive_director.py` get a stricter trigger set than every other role:

```yaml
human_escalation_triggers:
  - irreversible_external_action     # e.g. sending an email, spending money, deleting prod data
  - budget_or_timeline_commitment    # anything that promises the human a date or cost
  - legal_or_compliance_risk_acceptance
  - ambiguous_original_intent        # the original idea is genuinely underspecified,
                                      # not just under-decomposed
  - conflicting_stakeholder_input    # none exists yet in a single-human system, but
                                      # reserved for multi-stakeholder mode
  - explicit_human_request_to_be_asked  # human said "check with me before X" earlier
```

Anything else — including things a human might *want* to weigh in on but that Opus can reasonably decide and flag in the next status report — Opus resolves itself and logs the rationale (§8). This keeps the human's attention reserved for the small number of things that actually need it.

### 4.3 The escalation packet (data contract between tiers)

Every escalation, at every rung, uses the same envelope so the receiving tier doesn't need tier-specific parsing:

```json
{
  "escalation_id": "esc_9f2a...",
  "origin_role": "developer",
  "origin_tier": "T0",
  "origin_model": "qwen36",
  "task_id": "task_1284",
  "trigger": "retry_exhaustion",
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
  "escalation_chain": ["T0"]
}
```

Each tier that touches it *appends* to `escalation_chain` and either resolves it (writes a `resolution` field and sends it back down) or re-escalates (adds its own attempt + trigger and passes it up unchanged in shape). This makes the full path auditable after the fact regardless of how far up it went.

---

## 5. Human-in-the-loop chokepoint

```
        ┌─────────────────────────────────────────────┐
        │                    HUMAN                     │
        └───────────────────────▲───────────────────────┘
                                 │  (only via human_relay tool)
        ┌───────────────────────┴───────────────────────┐
        │      T3 — Claude Opus (Exec Dir / Client Comms) │
        │      re-classifies: "decide myself" vs "ask"    │
        └───────────────────────▲───────────────────────┘
                                 │ escalate
        ┌───────────────────────┴───────────────────────┐
        │      T2 — DeepSeek-V4-pro (via OpenCode)        │
        │      Product Owner / cross-cutting architecture │
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

`human_relay.py` is implemented as a **tool only Opus-tier agents can call**, enforced two ways (defense in depth, since the endpoint has no auth and role boundaries are otherwise just convention):

1. **Capability gating in code:** the tool is only registered in the toolset passed to agents whose `can_contact_human: true` (§3). T0–T2 agents never even see the tool exists.
2. **Runtime tier check:** `human_relay.call()` independently checks the caller's declared model against an allow-list (`claude-opus`) before it will actually deliver anything to the human-facing channel — so a misconfigured role can't accidentally get the capability just by editing one YAML field.

When invoked, `human_relay` doesn't just dump the raw escalation packet on the human — it hands it to `client_communications` (also T3) to render into a plain-language decision request:

> "The original idea didn't specify whether guest checkout should be allowed. This affects 3 stories already in progress. Which do you want: (a) require accounts, (b) allow guest checkout with email-only, (c) allow guest checkout with no data collection at all?"

The human's answer re-enters the system as a **decision record** in `project_memory`, tagged with the `escalation_id`, so it's available to every future task without re-asking (§8).

---

## 6. Routing mechanics

### 6.1 Router responsibilities

`model_backends/router.py` becomes the traffic cop with four jobs:

1. Resolve `role → default backend` from `models.yaml`.
2. On escalation, resolve `current_tier → next_tier` from that role's `escalation_ladder`.
3. Enforce local-GPU concurrency limits (§6.3).
4. Track and expose cost/latency metrics per tier so routing decisions (and later, budget alarms) can use real data instead of the static table in §2.2.

### 6.2 Tier selection within T0

`qwen36` vs `deepseek9b` isn't an escalation decision — it's a same-rung routing choice made once, at dispatch:

```python
def select_t0_model(task):
    if task.input_tokens > 10_000:       # deepseek9b's 1630 tok/s prompt speed wins here
        return "deepseek9b"
    if task.sla_seconds is not None and task.sla_seconds < 30:
        return "deepseek9b"              # faster prompt processing = faster time-to-first-token
    return "qwen36"                      # default: best quality/sec for typical bounded tasks
```

### 6.3 Local GPU contention

Since T0 (two model names) and T1 (one model name) all compete for the same 8 GB card:

- The router maintains a **single local-inference semaphore** (configurable concurrency, default 1–2 depending on observed VRAM behavior) shared across `qwen36`, `deepseek9b`, and `deepseek`.
- T1 requests are rare and slow by nature (4 tok/s) — they get **lower scheduling priority** than T0 requests, since blocking the whole developer/tester pipeline on one architect's hard question is the wrong trade. In practice: queue T1 requests and only dispatch them when no T0 tasks are pending, unless a T0 pipeline is itself blocked waiting on that specific T1 answer (in which case it's promoted).
- If the queue depth for local tasks exceeds a threshold (config: `local_queue_overflow`), the router is allowed to route *new* T0 work directly to T2 (DeepSeek-V4-pro) instead of queuing — trading a small dollar cost for throughput. This is a deliberate escape valve, logged distinctly from a normal escalation (`trigger: "capacity_overflow"`, not a capability failure) so it doesn't pollute the tuning signal from §1.5.

### 6.4 Skip-T1 fast path

As noted in §2.2, certain trigger types bypass T1 and escalate T0 → T2 directly:

- `security_flag` (NIST SSDF gate failures) — correctness matters more than cost here.
- Anything already tagged `human_escalation_triggers`-adjacent by a keyword/schema pre-check at T0 (e.g., the worker's own output mentions pricing, deletion, or an external send) — no point burning a slow T1 pass on something that's going to end up at T3 anyway.

### 6.5 Network/auth note

Because `10.42.0.83:8080` has no auth and is only reachable on the `10.42.0.0/24` LAN, the router process (and nothing else) should hold the only reference to that base URL. T2/T3 calls go out over the internet (OpenCode, `claude` CLI) from the same host, so no additional exposure is introduced there. If this harness is ever run from a machine off that subnet, T0/T1 become unreachable and the router should treat that as **all local tiers down** — not retry forever — and fall back per §6.6.

### 6.6 Local tier unavailable

If the local endpoint is unreachable (timeout/connection refused), the router treats it identically to "T0/T1 exhausted" and escalates straight to T2, logging `trigger: "tier_unavailable"`. This keeps the pipeline alive on a LAN hiccup at the cost of temporarily paying for work that's normally free.

---

## 7. Worker contract (unchanged from repo design, tightened here)

Every T0 dispatch uses a strict schema so escalation triggers (§4.1) can be detected mechanically rather than by re-reading prose output:

```json
{
  "status": "complete | needs_clarification | blocked",
  "output": { "...task-specific..." },
  "confidence": 0.0,
  "validation": { "self_check_passed": true, "notes": "" },
  "files_touched": ["..."],
  "reasoning_summary": "one paragraph, for audit log only"
}
```

`status != "complete"` or `confidence < min_confidence` triggers escalation automatically; the dispatcher never needs a separate "did this actually work" model call.

---

## 8. Audit & tuning loop

Every escalation packet (§4.3), every human_relay invocation, and every "Opus decided not to ask" resolution get appended to `compliance/audit_log.py`'s store, with:

- Full escalation chain and tier-by-tier attempts (cost/tokens per tier, so real $ cost per resolved issue is computable).
- Final resolution and which tier produced it.
- For human_relay calls specifically: the rendered question, the human's raw answer, and the decision record written back to `project_memory`.

This log is the feedback source for two ongoing tuning activities:
1. **Task-bound tuning:** roles/task types with high T0→T1/T2 escalation rates indicate `task_bounds.yaml` is splitting too coarsely for that task type.
2. **Ladder tuning:** if T1 (`deepseek`, 4 tok/s) resolves less than some threshold (e.g., 20%) of what it receives before further escalation, it's not earning its wall-clock cost and the ladder for that role should skip straight T0→T2.

---

## 9. Open questions / follow-ups

- **Concurrency ceiling for local models** isn't measured yet — needs a quick benchmark (dispatch 2 concurrent `qwen36` + `deepseek9b` requests and watch for OOM/thrashing) to set `local_queue_overflow` correctly rather than guessing.
- **Cost ceiling / budget alarm:** no dollar cap exists yet on T2/T3 usage; worth adding a `daily_spend_limit` that itself becomes a T3-only human-escalation trigger when approached.
- **Multi-human / stakeholder mode** is out of scope for v0.1 (single client assumption throughout) but the escalation packet schema was kept generic enough to extend later (`conflicting_stakeholder_input` trigger is already reserved).
