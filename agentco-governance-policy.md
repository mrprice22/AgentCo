# AgentCo — Governance, Policy & Compliance Mapping

**Design document v0.1**
**Scope:** The policy layer the escalation, data layer, and monitor/driver designs assume but never state: who is accountable, how much risk the system may take on its own, what data may leave the LAN, how agents are identified and protected from hostile content, how models and suppliers are governed, and how every control maps to NIST SSDF (SP 800-218/218A), the AI RMF (AI 100-1) and its Generative AI Profile (AI 600-1), and NIST SP 800-53 Rev. 5.

---

## 1. Why this document exists

The other three designs define *mechanisms*: a broker that enforces topic ACLs, a router that climbs a cost ladder, a monitor that surfaces decisions to the human. None of them say *who decided those rules, what the rules are allowed to be, or who answers for the outcome.* That's the job of governance, and it's also the first thing any NIST-aligned review asks for — SSDF's **PO** (Prepare the Organization) group and the AI RMF's **GOVERN** function both come before any technical control.

Two principles run through everything below:

1. **Policy is enforced by deterministic code, never by prompt alone.** Every policy here names an *enforcement point* — the broker, the router, the driver, CI — that works regardless of what a model outputs. Agent prompts restate relevant policy so models behave well, but a prompt is guidance, not a control. If the only thing stopping a behavior is a sentence in a `.jinja` file, that behavior is not controlled.
2. **Agents can be Responsible; only the human is Accountable.** No agent, including Opus, owns a risk. Opus holds *delegated* decision authority inside a risk tolerance the human has set (§4), and everything it decides under that delegation is reviewable after the fact (monitor design, §5.6).

---

## 2. Accountability

### 2.1 Named roles

| Governance role | Held by | Meaning |
|---|---|---|
| **System Owner / Authorizing Official** | The human | Sets policy and risk tolerance, accepts residual risk, approves system changes. The only accountable party. |
| **Business Owner** (SAFe) | The human | Owns outcomes and priorities for the product being built. |
| **Delegated Decision Authority** | Executive Director (T3) | Decides anything inside the risk tolerance without asking; must escalate anything outside it. |
| **Security Function** | Security Reviewer (T0) + deterministic scanners in CI | Checks artifacts against SSDF gates. Cannot waive a gate. |
| **Configuration & Change Authority** | Per change type — see change/config design, §4 | Never an agent for system changes. |
| **Operator** | The driver process (not an agent) | Runs, pauses, restarts, and records — no discretion. |

### 2.2 RACI

R = Responsible, A = Accountable, C = Consulted, I = Informed.

| Activity | Human | Exec Dir | PO | Architect | Security Rev | Dev / Tester | Driver |
|---|---|---|---|---|---|---|---|
| Set policy & risk tolerance | **A/R** | C | | | | | I |
| Approve system change (prompt, model, config, policy) | **A/R** | C | | C | C | | R (applies) |
| Accept a residual risk | **A/R** | C | | | C | | I |
| Product decision inside tolerance | A | **R** | R | C | | I | |
| Architecture decision | A | C | C | **R** | C | I | |
| Standard code change merge | A | | | | C | R | **R** (gate check) |
| Production release / deployment | **A/R** | R | C | C | C | | R (executes) |
| Answer a human-escalation | **A/R** | R (frames need) | | | | | R (records) |
| Incident response | A | R | | C | C | | **R** (detect/pause) |
| Review decisions made without asking | **A/R** | I | | | | | |

### 2.3 Separation of duties (800-53 AC-5)

- No artifact may be approved by the same sandbox instance that produced it. The driver enforces this by recording `producer_sandbox_id` on every artifact and refusing a gate result whose `reviewer_sandbox_id` matches.
- Developer, Tester, and Security Reviewer on a given task are always distinct roles *and* distinct sandbox instances.
- An agent may *propose* a change to the rules that constrain it (e.g., a retro suggests loosening a confidence threshold) but can never approve one — see the self-modification rule in the change/config design, §6.

---

## 3. Policy set

Each policy is a short, versioned statement plus machine-readable config where possible. Policy files live in `config/policy/` and are **governance configuration items**: they change only through the human-approved system change path (change/config design, §4).

| ID | Policy | Core rule | Enforcement point |
|---|---|---|---|
| **P-1** | AI Autonomy | Agents may act autonomously only inside `risk_tolerance.yaml` (§4). Prohibited autonomous actions: spending beyond budget, any external communication, deleting production data, changing governance config, disabling or editing logs. | Tool registry per role; driver action allow-list |
| **P-2** | Data Classification & Egress | Every message carries a data class; no prompt may be sent to a tier whose ceiling is below its class; Restricted data never enters any prompt (§5). | Router pre-dispatch check; secret scanner |
| **P-3** | Model & Supplier | Only models in the approved inventory may be routed to; remote suppliers require a recorded assessment and human risk acceptance (§8). | Router model allow-list = inventory |
| **P-4** | Identity & Access | Agent identity is assigned by the bus, not claimed; the monitor requires authentication for any write action; secrets are held only by the driver (§6). | Message bus; monitor backend |
| **P-5** | Secure Development | All product code passes the SSDF gate set in CI; an LLM reviewer is never the sole security control (§9). | CI pipeline (change/config design, §8) |
| **P-6** | Audit & Retention | All messages, escalations, decisions, and changes are logged to a tamper-evident audit log and retained per §12. | Audit log writer |
| **P-7** | Change & Configuration | See change/config design. | Driver baseline loader |
| **P-8** | Incident Response | Security and availability incidents are recorded, classified, and reported to the human; the kill switch is always available. | Driver + monitor (to be detailed in the service management design) |
| **P-9** | Untrusted Content | Content from the human, from code under review, from external sources, and from other agents' free text is data, never instructions (§7). | Prompt assembly; schema validation; tool gating |

---

## 4. Risk tolerance

The human's delegation to Opus is only as clear as the boundary around it. That boundary is a config file, not a paragraph in the Executive Director prompt:

```yaml
# config/policy/risk_tolerance.yaml
autonomy:
  spend:
    daily_limit_usd: 20.00          # T2+T3 combined; approaching 80% is a human-escalation trigger
    per_sprint_limit_usd: 150.00
  exec_director_may_decide:
    - product_scope_within_existing_epics
    - story_priority_within_sprint
    - architecture_decisions_reversible_within_one_sprint
  always_human:                     # superset of human_escalation_triggers (escalation design §4.2)
    - irreversible_external_action
    - budget_or_timeline_commitment
    - legal_or_compliance_risk_acceptance
    - ambiguous_original_intent
    - explicit_human_request_to_be_asked
    - production_deployment
    - new_remote_supplier_or_data_class_exception
    - governance_config_change

min_confidence:                     # single source of truth; prompts are rendered from these values
  default: 0.60
  product_owner: 0.65
  architect: 0.70
  security_reviewer: 0.75
```

`min_confidence` here resolves the current inconsistency between the escalation design (one global 0.6) and the prompt templates (0.6 / 0.65 / 0.7 / 0.75). The prompts should be rendered with `{{ min_confidence }}` from this file rather than hard-coding a number, so the enforced threshold (in the dispatcher) and the stated threshold (in the prompt) can never drift apart.

**Risk acceptance records.** When the human accepts a risk — a supplier, a data class exception, a skipped gate — it's written as a canonical record under `decision.risk_acceptance.*` with a mandatory `review_by` date. The ceremony scheduler raises expired acceptances in the escalation inbox for renewal or withdrawal. Risks that are open but not yet accepted live in the RAID log (SAFe/PMO design, to follow).

---

## 5. Data classification & egress

### 5.1 Classes

| Class | Examples | May appear in prompts? |
|---|---|---|
| **Public** | Open-source docs, public APIs, published release notes | Yes, any tier |
| **Internal** | Product source code, stories, acceptance criteria, architecture records, vision | Yes, up to each tier's ceiling |
| **Confidential** | Raw client communication, budget/pricing, business strategy, security findings, anything about real people | Yes, up to each tier's ceiling |
| **Restricted** | Credentials, API keys, private keys, tokens, production personal data | **Never.** Referenced by name only. |

Class is derived from **topic**, not declared by the sender — the same principle as the broker's ACL (data layer design, §5.2). `need_to_know.yaml` gains a `data_class` per topic:

```yaml
topic_classes:
  "vision":                         internal
  "story.*":                        internal
  "architecture.*":                 internal
  "decision.*":                     internal
  "decision.budget.*":              confidential
  "client.raw_communication":       confidential
  "security.finding.*":             confidential
default_class: confidential         # unlisted topics are treated as the most sensitive promptable class
```

A prompt's class is the **highest class of anything assembled into it**.

### 5.2 Tier ceilings

| Tier | Where data goes | Default ceiling | Condition |
|---|---|---|---|
| T0 / T1 | Local GPU host on the LAN | Confidential | — |
| T2 | DeepSeek-V4-pro via OpenCode (remote) | **Internal** | Raising it requires a completed supplier assessment (§8) and a human risk acceptance |
| T3 | Claude Opus via `claude` CLI (remote) | Confidential | Required by design — Client Comms and Exec Dir process raw client input — so the Anthropic supplier assessment is a prerequisite for operation, not an option |

**Routing consequence (amends escalation design §6):** before dispatch, the router compares the prompt class to the target tier's ceiling. If it's too high, the router skips to the next tier up whose ceiling allows it and logs `trigger: "data_class_ceiling"`. In practice, a Confidential task that fails at T1 goes straight to T3, which costs more. That's intended: the cost of egress control should show up on the cost dashboard, not stay hidden.

### 5.3 Egress safeguards

- **Secret scanning on every remote dispatch.** Every T2/T3 prompt passes through a pattern-and-entropy secret scanner before leaving the host. A hit blocks the call, records a security event, and escalates. The same scanner runs on every commit in CI (change/config design, §8).
- **No production personal data.** Agents work with synthetic or anonymized test data only. A product that will handle personal data in production gets that data classified Restricted at the boundary.
- **Egress logging.** Every remote call's audit entry records class, tier, supplier, byte count, and prompt hash — enough to answer "what left the building, and when" without storing the prompt twice.

---

## 6. Identity & access

### 6.1 Agent identity (800-53 IA-9, AC-4)

The data layer envelope's `from` field is currently set by the sender. Change: **the bus stamps `from`** based on which sandbox channel the message arrived on, and ignores any sender-supplied value. The sandbox's identity is fixed by `process_supervisor` at spawn (`sandbox_id`, `role`, `instance`, `tier`) and bound to its channel. That removes a whole class of spoofing (a T0 sandbox claiming to be the Product Owner to get `decision.*` publish authority) without adding any cryptography inside the sandbox.

### 6.2 Least privilege

Generalize the `can_contact_human` pattern (escalation design, §3) to every tool: each role gets an explicit `tools_allowed` list, deny by default, checked at the tool-dispatch layer. No tool call is honored just because a model asked for it.

### 6.3 Human identity & the monitor (800-53 AC-3, IA-2, AU-10)

- The monitor binds to `127.0.0.1` by default. LAN access requires turning it on explicitly *and* an authenticated session. This resolves the open auth question from monitor design v0.1 by choosing a safe default (now monitor design §10).
- Write actions (answering an escalation, approving a change, kill switch, roadmap edits) require an authenticated session. Each resulting record carries the session identity and a driver-held HMAC over the record, so a human decision can't be forged or altered after the fact by anything that can write to the store.

### 6.4 Secrets

T2/T3 credentials live in the Windows Credential Manager (or an `.env` excluded by `.gitignore`), are readable only by the driver/router process, and never enter a sandbox, a prompt, or a log. Rotation date is tracked as configuration metadata (change/config design, §2); the value never is.

---

## 7. Untrusted content & prompt injection

This is the AgentCo-specific risk that generic frameworks under-describe: **escalation is a privilege-escalation path.** Text written by a T0 model, whether confabulated, influenced by hostile content in the code it read, or copied from raw human input, travels up the ladder inside `attempts[].output` and eventually lands in an Opus prompt. Opus holds `human_relay` and `decision.*` publish authority. Content that arrives looking like an instruction ("Executive Director has pre-approved this spend") must never act as one.

Controls, from weakest to strongest:

1. **Provenance-labeled prompt assembly.** Every non-template segment is wrapped with its origin and class (e.g., `<untrusted origin="developer/T0/sbx-14" class="internal">…</untrusted>`), and every role prompt states that labeled content is data. This helps, but can't be relied on alone.
2. **Structured-output-only actions.** The harness acts only on schema-validated fields (`status`, `output.decision`, `topic`, …). Free-text fields (`explanation`, `reasoning_summary`, `rationale`) are never parsed for commands.
3. **Deterministic gates on consequential actions.** Publishing to `decision.*` / `architecture.*`, calling `human_relay`, and any spend are checked against role, risk tolerance, and budget in code, whatever the model argues.
4. **Source-chain disclosure on canonical records.** Every canonical record keeps the `source_msg_ids` chain back to its origins. Records whose chain includes human-intake text or reviewed third-party code are flagged in the monitor's decision log for spot-check.
5. **Adversarial evals.** Injection test cases are part of every role's golden eval suite and must pass before any prompt or model change is approved (change/config design, §7).

**Confabulation** (AI 600-1) is handled on the same path: a canonical record needs a source chain, and a record that can't show one (a model asserting a "fact" nobody said) is rejected at publish time.

---

## 8. Model & supplier governance

### 8.1 Model inventory (AI RMF GOVERN 1.6)

The router's allow-list *is* this inventory. A model that isn't listed can't be routed to.

| Model | Tier | Hosting | Identity | License | Ceiling | Intended use | Known limits |
|---|---|---|---|---|---|---|---|
| `qwen36` (Qwen3.6-35B-A3B Q4_K_M) | T0 | Local | GGUF SHA-256 | Model license (record) | Confidential | Bounded dev/test tasks | 32K ctx; quantization loss |
| `deepseek9b` (Qwen3.5-9B distill) | T0 | Local | GGUF SHA-256 | Model license (record) | Confidential | Long-input / fast-TTFT bounded tasks | 16K ctx |
| `deepseek` (DeepSeek-V4-Flash UD-IQ1_S) | T1 | Local | GGUF SHA-256 | Model license (record) | Confidential | Single hard technical questions | ~1-bit quantization: expect measurable reasoning degradation; ~4 tok/s |
| DeepSeek-V4-pro | T2 | Remote (OpenCode) | Provider model ID + date | Provider ToS | Internal | Product/architecture reasoning | Silent provider-side updates; data handling per assessment |
| Claude Opus | T3 | Remote (`claude` CLI) | Provider model ID + date | Provider ToS | Confidential | Exec decisions, human-facing text | Silent provider-side updates |

Each row links to a short **model card** (`config/models/<id>.md`): source URL, download date, hash, license text, eval results by role, and the date of last review. Adding, replacing, or re-quantizing a model is a system change requiring an eval pass (change/config design, §7).

### 8.2 Supplier assessment (AI RMF GOVERN 6, MAP 4, MANAGE 3; 800-53 SA-9, SR-3)

Required before a remote supplier is routed to, and reviewed on a schedule:

- Data retention and whether prompts may be used for training
- Jurisdiction and data-location terms
- Rate limits, availability history, and what AgentCo does when the supplier is down (service management design)
- Pricing and change-notice terms
- For intermediaries (OpenCode): what the intermediary itself sees and stores, separate from the model provider

The result is a `decision.risk_acceptance.supplier.*` record signed by the human, carrying the ceiling granted (§5.2) and a `review_by` date.

### 8.3 Supply chain for local components (800-53 SR-4, SR-11, SI-7)

Model weights, the llama.cpp-style server build, the `claude` CLI, and OpenCode are all third-party components. Each is pinned by version and (where possible) hash as a configuration item, verified at driver startup, and only updated through a change record.

### 8.4 Intellectual property (AI 600-1)

- Dependencies introduced by Developer agents are license-scanned in CI against an allow-list maintained in `config/policy/licenses.yaml`.
- Model weight licenses are recorded on the model card and checked against intended use before onboarding.

---

## 9. Secure development — SSDF mapping (SP 800-218)

**Principle:** the Security Reviewer is a T0 model. It interprets scanner output, applies judgment to borderline cases, and escalates, but it is **never the only control** for any SSDF task. Deterministic tools run first in CI, and the LLM reviews their output together with the diff.

| Practice | AgentCo implementation | Where |
|---|---|---|
| **PO.1** Define security requirements | Security NFRs as backlog items; per-project `ssdf_gates.yaml` | This doc; SAFe design (NFRs) |
| **PO.2** Roles & responsibilities | §2 RACI | This doc |
| **PO.3** Supporting toolchains | CI pipeline, scanners, pinned as CIs | Change/config design §2, §8 |
| **PO.4** Criteria for security checks | `gate_criteria` per SSDF practice, versioned | `config/policy/ssdf_gates.yaml` |
| **PO.5** Secure environments | Sandbox isolation, host hardening, monitor auth, secret handling | Data layer §5.4, §9; this doc §6 |
| **PS.1** Protect code from tampering | Driver is sole committer; branch protection; signed commits | Change/config design §8 |
| **PS.2** Release integrity verification | Signed tags, checksums, SBOM | Change/config design §9 |
| **PS.3** Archive & protect releases | Release archive with provenance incl. `baseline_id` | Change/config design §9 |
| **PW.1** Design to mitigate risk | Architect produces a threat model per epic; reviewed by Security Reviewer | SAFe design (enabler stories) |
| **PW.2** Review the design | Architecture decisions reviewed before canonical publish | Architect prompt; change/config §4 |
| **PW.4** Reuse well-secured software | Dependency allow-list; SCA on new deps (normal change) | Change/config §4, §8 |
| **PW.5** Secure coding practices | Developer prompt standards + linters | Prompts; CI |
| **PW.6** Secure build configuration | CI build definitions as CIs | Change/config §8 |
| **PW.7** Review/analyze code | SAST + secret scan → Security Reviewer | CI → Security Reviewer |
| **PW.8** Test executable code | Unit + Tester-generated tests in CI | Tester prompt; CI |
| **PW.9** Secure defaults | Checklist in `ssdf_gates.yaml` | Security Reviewer |
| **RV.1** Identify vulnerabilities continuously | Scheduled SCA scan of every active product repo | Ceremony scheduler |
| **RV.2** Assess, prioritize, remediate | Findings become backlog items with remediation SLA by severity | Service management design |
| **RV.3** Root-cause analysis | Problem records for repeat findings | Service management design |

**SP 800-218A** (the SSDF community profile for generative AI and dual-use foundation models) mostly targets model *producers*. AgentCo is a model *consumer*, so its relevant parts are narrower: protect model weights and configuration from tampering (§8.3), and treat model output as untrusted input to the build (§7).

---

## 10. AI RMF & Generative AI Profile mapping

### 10.1 AI RMF functions (AI 100-1)

| Function | Category focus | AgentCo artifact |
|---|---|---|
| **GOVERN** | Policies & processes (GV-1), accountability (GV-2), third parties (GV-6) | This document: policies §3, RACI §2, supplier assessment §8.2; model inventory §8.1 |
| **MAP** | Context & intended use (MP-1), component risks incl. third-party (MP-4) | Model cards (intended use, limits); data classification §5; threat models per epic |
| **MEASURE** | Methods & metrics (MS-1), evaluation of trustworthiness (MS-2), tracking over time (MS-3) | Golden eval suites & canaries (change/config §7); escalation-rate and T1 resolution-rate metrics (escalation design §8) |
| **MANAGE** | Prioritize & respond (MG-1), third-party risk (MG-3), post-deployment monitoring & response (MG-4) | Risk acceptances with review dates §4; decision log review; incident process; kill switch |

### 10.2 Generative AI risks (AI 600-1) most relevant here

| Risk | How it shows up in AgentCo | Primary control |
|---|---|---|
| **Confabulation** | A small model's invented "fact" becomes a canonical record other agents trust | Source-chain requirement at publish (§7); publish authority (data layer §5.3) |
| **Information security** | Prompt injection via intake, reviewed code, or escalation packets; secret leakage in remote prompts | §7 controls; secret scanning §5.3 |
| **Data privacy** | Client communication or personal data sent to remote suppliers | Classification & tier ceilings §5 |
| **Human-AI configuration** | The human over-trusts "resolved without you" decisions (automation bias), or is flooded with escalations (alert fatigue) | Decision log review cadence (§13); escalation-rate SLOs (service management design) |
| **Information integrity** | Status reports that misstate progress | Client Comms reports generated from canonical records + metrics, not free recall |
| **Intellectual property** | Generated code reproducing licensed code; model weight license limits | License scanning §8.4 |
| **Value chain & component integration** | Silent remote model updates; unverified local weights | Inventory + hashes §8; drift canaries (change/config §3) |

---

## 11. NIST SP 800-53 Rev. 5 — selected controls

This isn't a formal baseline selection (AgentCo is a single-operator system with no authorization boundary to certify). It's the control catalog used as a checklist, so a gap is visible when one exists.

| Control | Implementation | Status |
|---|---|---|
| AC-3 Access enforcement | Broker topic ACL | Designed (data layer §5) |
| AC-4 Information flow enforcement | Broker + tier ceilings | Designed + this doc §5.2 |
| AC-5 Separation of duties | Producer ≠ reviewer | This doc §2.3 |
| AC-6 Least privilege | Per-role `tools_allowed` | This doc §6.2 |
| AU-2/3/12 Event logging & content | Audit log | Designed (escalation §8) |
| AU-9 Protection of audit information | Hash-chained log, external anchor | This doc §12 |
| AU-10 Non-repudiation | HMAC'd human decision records | This doc §6.3 |
| AU-11 Audit record retention | Retention table | This doc §12 |
| CM-2/3/5/8 | Baselines, change control, inventory | Change/config design |
| CP-9/10 Backup & recovery | Store backups, baseline rollback | Change/config §11; service management design |
| IA-2 Identification (users) | Monitor authentication | This doc §6.3 |
| IA-9 Service identification | Bus-stamped agent identity | This doc §6.1 |
| IR-4 Incident handling | Incident process | Service management design (gap) |
| RA-3 Risk assessment | RAID log + risk acceptances | This doc §4; SAFe/PMO design |
| SA-9 External system services | Supplier assessment | This doc §8.2 |
| SA-11 Developer testing | CI gates | Change/config §8 |
| SC-7 Boundary protection | LAN-only endpoint, localhost monitor | Designed + this doc §6.3 |
| SC-12 / SC-28 Key mgmt / data at rest | Credential Manager; store file ACLs | This doc §6.4 (at-rest encryption: open) |
| SI-4 System monitoring | Event tap, dashboards, alerts | Monitor design; service management design |
| SI-7 Software & information integrity | Hash-verified models & configs | This doc §8.3; change/config §3 |
| SI-10 Information input validation | Schema-validated outputs; provenance labeling | This doc §7 |
| SR-3/4/11 Supply chain | Pinned, verified components | This doc §8.3 |

---

## 12. Audit evidence, integrity & retention

**Control tagging.** Every audit entry carries the controls it evidences, so the compliance dashboard (monitor design §5.12) can report per control without guessing:

```json
{
  "entry_id": "aud_77c1...",
  "event": "gate_result",
  "task_id": "task_1284",
  "baseline_id": "bl_2026_09_27_a",
  "controls": ["SSDF:PW.7", "800-53:SA-11", "AIRMF:MEASURE-2"],
  "prev_hash": "sha256:9b1e...",
  "entry_hash": "sha256:41fa..."
}
```

**Tamper evidence (AU-9).** `entry_hash = SHA-256(prev_hash ‖ canonical_json(entry))`. Once a day the driver writes the current chain head into the human's status report. The human's own inbox then holds an external anchor that nothing inside AgentCo can rewrite. Verification is a one-command replay of the chain.

**Retention (AU-11).** Proposed defaults, set in `config/policy/retention.yaml`:

| Store | Retention |
|---|---|
| Audit log | Life of project + 3 years |
| Canonical records | Life of project + 3 years (append-only; never pruned) |
| Episodic logs (per agent) | Life of project + 1 year |
| Semantic notes | Life of project; superseded chains compacted per data layer §10 |
| Egress records (§5.3) | 1 year |
| Live feed (if persisted) | 90 days |

---

## 13. Review cadence

- **Every sprint:** the human spot-checks the monitor's "resolved without you" decision log. This keeps the delegation to Opus calibrated and counters automation bias.
- **Every PI / quarter:** a governance review ceremony, scheduled by the ceremony scheduler and delivered as an inbox item. It covers risk acceptances nearing `review_by`, supplier assessments, model inventory, escalation-rate trends, and policy changes proposed by retros.
- **On any incident:** the policies the incident touched are reviewed as part of its post-incident review.

---

## 14. Changes required to existing documents

| Document | Change |
|---|---|
| Escalation design §3, §6 | Router checks data-class ceiling before dispatch; new trigger `data_class_ceiling`; per-role `min_confidence` sourced from `risk_tolerance.yaml`; `tools_allowed` per role |
| Escalation design §8 | Audit entries carry `controls`, `baseline_id`, and hash chain fields |
| Data layer design §4 | Envelope gains `data_class` (derived from topic); `from` is stamped by the bus, not the sender |
| Data layer design §5.1 | `need_to_know.yaml` gains `topic_classes` |
| Monitor design | Localhost-by-default + authenticated sessions for write actions (resolves open question) — **done in monitor design v0.2** (§10) |
| All prompt templates | Render thresholds from `{{ min_confidence }}`; add the untrusted-content handling statement |

---

## 15. Open questions / follow-ups

- **T2 ceiling:** whether DeepSeek-V4-pro via OpenCode can be raised above Internal depends on the supplier assessment. Until then, Confidential work skips T2, and the cost impact of that should be measured.
- **Effectiveness of provenance labeling on small local models** is unproven; the injection eval suite should measure it before relying on it for anything.
- **Data-at-rest encryption** for SQLite stores (SC-28): OS-level disk encryption may be enough for a single host; not yet decided.
- **Multi-client use:** if AgentCo ever builds for more than one client, classification needs a per-client dimension (tenant isolation), which this version doesn't model.
