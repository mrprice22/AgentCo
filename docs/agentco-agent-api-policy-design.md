# AgentCo — Agent API, RACI & Policy Manager

**Design document v0.1**
**Scope:** How every agent is confined and governed:
- **the Agent API:** the only interface an agent sandbox has
- **the RACI matrix:** the source every agent permission is generated from
- **the change and release path**, which makes "a developer can't reach production" true by construction rather than by instruction
- **the policy manager:** every agent works under a locked **engagement** (a policy, a contract, and a scope of work) and can't make a single model call until that engagement is locked

This implements the owner's sandbox decision (2026-09-27): *each agent runs in a sandbox and can only interact through an API with ACLs based on the agent's role.*

---

## 1. The principle: can't, not shouldn't

The earlier designs constrain agents in three ways: by prompt (role instructions, the company directive), by the broker's topic ACL (data layer design §5), and by the engine applying only validated proposals (core engine design §1). This document adds the fourth and most direct: **an agent can only do what its API permits, and its API permits only what its role's RACI assignments and its locked engagement allow.**

A Developer is told not to deploy to production. That's the directive and the prompt. More importantly, the Developer's API has **no verb** that deploys, merges, or approves anything. There's nothing to misuse, no matter what the model outputs or what hostile content it has read.

---

## 2. The sandbox and its one interface

### 2.1 Isolation

Each agent instance runs in its own sandbox:

- **v1 mechanism:** one OS process per sandbox, started by the process supervisor under a restricted local account, with its own empty working directory. Containers remain an option later without changing anything in this document, because everything here is defined at the API boundary (data layer design §10).
- **No filesystem path** to any store, repository, config, baseline, or other sandbox. **No secrets:** T2/T3 credentials stay with the driver (governance design §6.4).
- **One network destination:** the Agent API endpoint on loopback, authenticated with a per-sandbox credential issued at spawn. A host firewall rule denying all other outbound traffic for the sandbox account is defense in depth, not the primary control. The primary control is that there's nothing else to reach.

### 2.2 Model calls go through the API too

A sandbox doesn't talk to any model directly. `model.complete` is an Agent API verb that the gateway hands to the router. That's where tier selection, the data-class ceiling, the budget, the Start/Pause run state (monitor design §4.2), and the **engagement lock** (§6) are enforced, all in one place and before any tokens are spent.

### 2.3 How a job runs

The job cycle (core engine design §8) is unchanged in shape. The difference is that every step is an API call that is checked:

```
engagement.bind ──▶ job.lease ──▶ (record.get / kb.search / code.read / notes.query …)
      ──▶ model.complete (one or more) ──▶ (cr.create / mail.send / file.put … if permitted)
      ──▶ job.submit(output) ──▶ engine validates and applies effects
```

**Every call is checked in the same order:**
1. **Identity:** the sandbox credential determines the caller. A caller can't claim a different role (governance design §6.1).
2. **Run state:** paused means no leases and no model calls.
3. **Engagement:** bound, locked, active, unexpired, and the hash matches the baseline (§6).
4. **Permission:** the verb is in the role's generated permission set (§4).
5. **Scope:** the object is within the engagement's scope of work (products, paths, topics, budget).
6. **Object state:** workflow guards (core engine design §5.2); for example, a transition is only offered from the states that allow it.
7. **Data class:** the content's class is within the caller's ceiling (governance design §5).

A denied call returns a reason code to the agent and writes an audit entry. Repeated denials from one sandbox open an incident, because they're a common signature of prompt injection (governance design §7).

---

## 3. The Agent API

Verbs are grouped by resource. *Who may call* is never hard-coded: it's generated from the RACI matrix (§4). The right-hand column shows the typical holders.

| Resource | Verbs | Typical holders |
|---|---|---|
| **Engagement** | `engagement.bind`, `engagement.get` | Every sandbox (`bind` is the *only* verb an unbound sandbox has) |
| **Jobs** | `job.lease`, `job.heartbeat`, `job.submit` | Every bound sandbox, for its own jobs only |
| **Models** | `model.complete` | Every bound sandbox; tier and ceiling are set by the job type and engagement |
| **Memory** | `notes.query`, `notes.write` | Own store only (data layer design §3.3) |
| **Records** | `record.get`, `record.publish` | `get` filtered by need-to-know; `publish` by publish authority (data layer design §5.3) |
| **Work items** | `item.get`, `item.create`, `item.transition`, `item.comment` | Create and transition limited per kind and state pair (§5.1) |
| **Change requests** | `cr.create`, `cr.update_own`, `cr.withdraw_own`, `cr.assess`, `cr.approve` | Create: delivery roles. Assess: change coordinator. Approve: **only** the RACI "A" for that change type, and **never** the CR's author |
| **Code** | `code.read`, `code.submit_diff` | Read within scope paths; submit goes to the driver, which commits to a task branch after its scope check (change/config design §8.2) |
| **Pipeline** | `ci.status` | Read-only. **No agent verb merges or deploys.** |
| **Releases** | `release.propose`, `release.add_change`, `release.status` | Change coordinator. Approval of a production release is a **console command**, not an API verb (§4.3) |
| **Escalation** | `escalate`, `refer` | Every role, with a reason from its enum (core engine design §9); requires a knowledge search first (knowledge design §5) |
| **Knowledge** | `kb.search`, `kb.draft`, `kb.review`, `kb.publish` | Search: everyone. Draft, review, publish per RACI (knowledge design) |
| **Collaboration** | `mail.send`, `mail.read`, `meeting.request`, `meeting.contribute`, `file.put`, `file.get`, `file.share` | Per RACI and need-to-know (knowledge design §7–§9) |
| **Human** | `human_relay` | Executive Director and Client Communications only (escalation design §5) |

Two properties hold across the whole surface:
- **Reads are need-to-know filtered; writes are proposals.** Nothing an agent calls mutates engine state directly. The gateway turns an allowed write into an engine event, and the engine applies it under its own rules (core engine design §1).
- **Verbs are narrow.** There's no generic "update item" or "run command." A new capability means a new verb, and adding a verb is a system change (§7).

---

## 4. RACI: the source of permissions

### 4.1 `config/raci.yaml`

The governance design's RACI (§2.2) covers the human-level decisions. This matrix covers **every process activity**, and it's machine-readable, because agent permissions are generated from it:

```yaml
# config/raci.yaml (excerpt)
activities:
  implement_task:
    kind: perform                 # verbs go to R
    verbs: [code.read, code.submit_diff, "item.transition:task:in_progress->in_review"]
    R: [developer]
    A: architect
    I: [tester]

  raise_change_request:
    kind: perform
    verbs: [cr.create, cr.update_own, cr.withdraw_own]
    R: [developer, tester, architect, security_reviewer]
    A: architect
    I: [change_coordinator]

  approve_change_normal_minor:
    kind: approval                # the approve verb goes to A, never to R
    verbs: ["cr.approve:normal_minor"]
    R: [change_coordinator]       # prepares the approval package
    A: architect
    C: [security_reviewer]
    requires_sod: true            # A may never approve a CR they authored

  approve_production_release:
    kind: approval
    verbs: ["release.approve:production"]
    R: [change_coordinator]
    A: owner                      # A = owner ⇒ console command only, no agent verb exists
    C: [executive_director, tester]
    requires_sod: true
```

### 4.2 Generation rules

| RACI letter | Grants |
|---|---|
| **R** on a `perform` activity | The activity's verbs |
| **A** on an `approval` activity | The approve verb, with separation of duties enforced: the approver can't be the object's author or producer (governance design §2.3) |
| **C** | `item.comment` on the activity's objects; a consult request is sent when the activity starts |
| **I** | A notification subscription (mail or digest) |

Lint rules run in CI for the AgentCo repo (change/config design §10):
- exactly one **A** per activity
- every verb in the API is covered by at least one activity, so there are no orphan capabilities
- `requires_sod` is set on every approval
- no agent is **A** on an activity the governance design reserves for the human

The output, `permissions.lock` (role → verbs, with parameters), is a **configuration item in the baseline**. Changing the RACI matrix changes what agents can do, so it's a system change, approved by the owner only (change/config design §4.2).

### 4.3 When the owner is "A"

When an activity's accountable party is the owner, the approve verb **isn't generated for any agent at all**. It exists only as a console command behind the trusted confirmation dialog (console design §3.3). Examples: production releases, system changes, risk acceptances. No sequence of agent calls can reach these approvals, including a successful injection into the Executive Director.

### 4.4 The matrix

A = accountable (exactly one), R = responsible, C = consulted, I = informed. **Coord** is the change & release coordinator (§5.3). **Driver** is deterministic plumbing, not an agent.

| Activity | Owner | ED | CC | PO | Arch | Dev | Test | Sec | SM | Coord | Driver |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Capture intake | A | I | R | I | | | | | | | |
| Set vision | A | R | C | C | | | | | | | |
| Refine backlog | | A | | R | C | | | | I | | |
| Decompose story | | | | A | R | C | I | | | | |
| Architecture decision | | A | | C | R | I | | C | | | |
| Implement task | | | | | A | R | I | | | | |
| Security gate | | A | | | C | I | | R | | | |
| Test and verify | | | | A | | I | R | | | | |
| Raise change request | | | | | A | R | R | R | | I | |
| Assess change | | | | | C | | | C | | R | |
| Approve standard change | A | | | | I | | | | | | R |
| Approve normal-minor change | | | | | A | I | | C | | R | |
| Approve normal-major change | I | A | | C | C | | | C | | R | |
| Merge to trunk | | | | | A | I | I | | | | R |
| Assemble release | | | | A | C | | C | | I | R | |
| Approve production release | A | C | | I | | | C | | | R | |
| Deploy to production | A | I | | | | | C | | | C | R |
| Incident response | A | I | | | C | | | C | I | | R |
| Problem root cause | I | A | | | R | | | C | C | | |
| Author knowledge article | | | | | C | R | R | R | A | | |
| Capture and close lessons | I | A | C | C | C | C | C | C | R | C | |
| Facilitate meetings | | | | | | | | | A | | R |
| Maintain own role dashboard | I | R | R | A | R | R | R | R | R | R | |
| Author engagement templates | A | R | | | C | | | | C | | |
| Lock an engagement instance | A | C | | | | | | | | | R |
| Change RACI or permissions | A | R | | | C | | | C | | | |
| Start / Pause the simulation | A/R | | | | | | | | | | I |

**"Author knowledge article"** means R for whichever role resolved the issue, with the Scrum Master accountable as knowledge manager. **"Approve standard change"** has the owner as A because the owner approved the list of pre-authorized change types; the driver applies it mechanically.

---

## 5. The path to production

### 5.1 Transition permissions

`item.transition` is generated per role as a list of allowed `(kind, from → to)` pairs. The engine's guards still apply on top. Excerpt:

| Role | May request |
|---|---|
| Developer | `task: in_progress → in_review` (submit), `task: in_progress → blocked` (with reason) |
| Tester | Records a verdict (`pass`/`fail`), which drives `in_test → done` or `in_test → in_progress`. May not set `done` directly. |
| Architect | `story → needs_refinement`; task creation through decomposition |
| Product Owner | `story: draft → ready` (subject to the DoR guard), `any → cancelled` |
| Change coordinator | `cr: proposed → assessed`, `release: planning → candidate` |
| **Nobody** | `→ done`, `→ merged`, `→ deployed`. Only the engine and driver set these, when their guards are met. |

### 5.2 One change, end to end

The owner's example: *a developer can't commit directly to production; the work has to go to their tester, pass the full pipeline, and be deployed in a release with other updates, approved by change management.*

1. **Developer** implements the task and calls `code.submit_diff`. The **driver** checks the diff's scope and commits it to `task/<id>`. The Developer can't commit anywhere, least of all trunk or production.
2. **CI** runs (change/config design §8.3). The **Security Reviewer** records its gate result.
3. The engine moves the task to `in_test` and leases a **Tester** job. The Developer has no verb to skip this or mark it tested. A fail verdict returns the work to the Developer with the failures attached.
4. On pass, the change is **merged to trunk** by the driver, as a standard change if it qualifies, or after the right **A** approves the CR (§4.4).
5. The **change coordinator** adds approved, merged changes to a **release candidate** alongside other updates, with the evidence bundle (tests, scans, SBOM, change records).
6. The release is deployed to **staging** automatically, and Tester-generated smoke tests run.
7. **Approval for production** is requested from the **owner**, acting as change manager, in the console inbox. It's a console command behind the trusted dialog, and no agent verb exists for it (§4.3).
8. The **driver** deploys, runs smoke tests, and rolls back automatically on failure (change/config design §9).

At no step does any agent hold a capability that could shortcut the next one.

### 5.3 The change & release coordinator (new role)

Change management needs someone to *do* its paperwork without being able to *approve* anything. That's a new agent role, **`change_coordinator`** (default tier T0; T2 for release notes and impact summaries):

- **R:** CR completeness checks, compiling impact assessments (from the configuration item relationships in change/config design §2), assembling releases, change schedule and freeze windows, and the approval package the owner sees.
- **Never A.** It holds no approve verb for any change type.

This matches ITIL 4's separation between *change enablement* (the practice) and *change authority* (who approves). The owner is the change authority for production and system changes, the Architect and Executive Director for normal product changes, and the pre-authorized list for standard changes.

---

## 6. Policy manager: engagements

### 6.1 Every agent works under an engagement

An **engagement** binds three documents to one agent instance, the way a contractor works under a policy, a contract, and a statement of work:

| Part | Answers | Contents |
|---|---|---|
| **Policy** | *What rules do I follow?* | Directive version; applicable governance policies (P-1…P-9); the generated permissions for its role; risk tolerance thresholds (e.g. `min_confidence`); data-class ceiling; escalation and referral map |
| **Contract** | *What do I owe, and what am I owed?* | Deliverables (output types, write-back notes, dashboard upkeep); quality obligations (output schema, Definition of Done); OLAs; standing obligations (search knowledge before escalating, cite metric IDs); breach handling; what the company provides (context under need-to-know, tools, budget) |
| **Scope of work** | *On what, and how much?* | Products; job types; repositories and paths; topics; token and dollar budget per iteration; time window; explicit out-of-scope list |

```yaml
engagement_id: eng-dev-sbx14-it42
version: 1
template: "engagements/developer.yaml@blob:9c1e…"
baseline_id: bl_2026_10_04_a
bound_to: { sandbox_id: sbx-14, role: developer, instance: 2 }
policy:
  directive: v1.1
  governance_policies: [P-1, P-2, P-4, P-5, P-9]
  permissions: "permissions.lock#developer@blob:44aa…"
  min_confidence: 0.60
  data_class_ceiling: confidential
contract:
  deliverables: [diff, explanation, write_back_notes, dashboard_commentary]
  quality: ["schema:schemas/developer.output.json", "definition_of_done:task"]
  olas: { implement_task_p90_minutes: 5 }
  obligations: [search_knowledge_before_escalating, cite_metric_ids]
  breach_handling: "Repeated breaches open a problem record; the engagement may be suspended pending review."
scope_of_work:
  products: [client-app-1]
  job_types: [implement_task, maintain_dashboard, meeting_contribution]
  repos: { client-app-1: { paths: ["src/**", "tests/**"] } }
  topics: ["story.*.acceptance_criteria", "architecture.*", "kb.engineering.*"]
  budget: { tokens_per_iteration: 400000, usd_per_iteration: 2.00 }
  window: { iterations: [42] }
  out_of_scope: ["production deployment", "data migrations", "new dependencies without a change request"]
lock:
  hash: "sha256:7b2e…"
  locked_at: "2026-10-04T08:00:03Z"
  locked_by: "driver (template approved in chg_0051)"
  hmac: "…"
```

### 6.2 Templates and instances

- **Templates** (`config/engagements/<role>.yaml`) are governance configuration items. The Executive Director drafts them and the owner approves them as system changes (RACI: *Author engagement templates*).
- **Instances** are **derived deterministically** from an approved template plus an assignment (product, iteration, instance number). Because derivation is mechanical, a routine instance needs no per-instance approval: its approval *is* the template's. Anything a template doesn't cover needs an explicit approval before it can lock, with the ED as C and the owner as A. Examples: cross-product scope, a budget above the product's share, or a data-class ceiling above the template's.

### 6.3 Lifecycle

```
draft ──▶ validated ──▶ locked ──▶ active ──▶ expired
                                     │   └──▶ superseded (new version; sandbox drains, then rebinds)
                                     └──────▶ revoked (immediate; sandbox is stopped)
```

**Validated** means:
- the schema is valid
- permissions match `permissions.lock` for the role
- the data-class ceiling is ≤ every tier the job types can route to (governance design §5.2)
- the budget fits within the product's capacity share (console design §8.2)
- the scope's topics are within the role's need-to-know ACL

**Locked** means the content hash is recorded, the driver HMAC is applied, and the engagement is published as a canonical record under `engagement.*`.

### 6.4 No policy, no inference

This implements the owner's requirement that *agent LLMs can't kick on before their policy is locked in*:

1. The supervisor spawns every sandbox **unbound**. Its only credential allows exactly one verb: `engagement.bind`.
2. `engagement.bind` succeeds only if a **locked, active, unexpired** engagement exists for that sandbox's assignment, and its hash matches the template blob in the running baseline. The gateway then marks the sandbox bound to that `engagement_id` and hash.
3. The router refuses `model.complete` from any sandbox that isn't bound, or whose engagement has since expired, been revoked, or been superseded. **No engagement, no tokens.**
4. The prompt renderer inserts an **engagement summary** (policy, contract obligations, scope, and out-of-scope list) directly after the company directive in every prompt. The model *knows* its terms, and the gateway *enforces* them. Following governance design §1, the text is guidance and the gate is the control.
5. Every other verb re-checks the engagement's scope on each call, so a sandbox bound for `client-app-1` can't read `client-app-2`'s repository even if the model tries.

Engagement budgets complement the owner's **Start / Pause** control. Pause stops everything at once; engagement budgets stop any single agent from consuming a product's share while the company is running.

### 6.5 Where the engagement sits in precedence

The company directive's precedence (company directive §5) becomes:

**system controls → directive → engagement policy → role instructions → task**, with content never ranking at all.

The engagement ranks above the role prompt because it's the approved, locked, instance-specific statement of the rules. The role prompt describes *how* to do the job; the engagement states *what's permitted in this assignment.*

---

## 7. Governance of the API itself

| Change | Class | Approver |
|---|---|---|
| New or changed Agent API verb | System-normal | Owner |
| RACI matrix / `permissions.lock` | System-normal | Owner |
| Engagement template | System-normal (eval gate runs the role's suite) | Owner |
| Engagement instance within template | Automatic on lock | (Template approval) |
| Engagement exception (scope beyond template) | Explicit approval | Owner (ED consulted) |

The console shows each agent's **terms** (engagement, permissions, and RACI rows) on its Org chart card (console design §5.1). The engagement templates and the RACI matrix are **protected pages**: agents can propose changes to them but can't edit or restyle them (console design §3.4).

---

## 8. Audit and metrics

Every Agent API call is logged with verb, sandbox, engagement, decision (allow/deny), reason, and correlation ID.

Registry metrics (console design §7.1):
- denial rate per role and verb (high rates suggest a prompt problem or an injection attempt)
- engagement lock latency (spawn → bound)
- engagement breaches per role
- budget consumption against engagement budgets
- change coordinator package completeness (approvals returned for missing evidence)

---

## 9. Control mapping

| Framework | Reference | Section |
|---|---|---|
| 800-53 | AC-2 Account management (sandbox credentials, bind lifecycle) | §2, §6.4 |
| 800-53 | AC-3 Access enforcement / AC-6 Least privilege | §3, §4 |
| 800-53 | AC-5 Separation of duties | §4.2, §5 |
| 800-53 | PL-4 Rules of behavior (the engagement policy) | §6 |
| 800-53 | PS-6 Access agreements (the engagement contract) | §6 |
| 800-53 | CM-3 / CM-5 Change control; access restrictions for change | §5, §7 |
| SSDF | PO.2 Roles and responsibilities | §4 |
| AI RMF | GOVERN 2 (accountability structures), MANAGE 2 (mechanisms to supersede or deactivate) | §4, §6.3 |
| ITIL 4 | Change enablement (coordinator vs. change authority), Release management, Relationship management | §5 |

---

## 10. Changes required to existing documents

| Document | Change |
|---|---|
| Data layer design §2, §5.4 | "Message bus is the only I/O" becomes "the Agent API is the only I/O; `mail.send` and publish are verbs on it". Topic `engagement.*` added |
| Escalation design §3 | `tools_allowed` is replaced by generated permissions (`permissions.lock`) |
| Core engine design §3.1, §8 | Org chart gains `change_coordinator`; the job cycle starts with `engagement.bind`; the prompt renderer inserts the engagement summary |
| Governance design §2.2, §6.2 | RACI there becomes the human-level summary of `config/raci.yaml`; least privilege is implemented by generated permissions |
| Change/config design §4, §9 | Change coordinator role; release assembly; production approval is an owner console command |
| Monitor design §4.2 | Supervisor spawns sandboxes unbound; binding and revocation states appear in Agent health |
| Company directive §5 | Precedence gains the engagement (§6.5 here) |
| Prompt templates | Engagement summary block after the directive include; a new `change_coordinator.jinja` |

---

## 11. Open questions / follow-ups

- **Instance-level vs. role-level separation of duties** for the Architect, which is both the default approver of normal-minor changes and a frequent CR author. §4.2's SoD check blocks self-approval. Whether that sends those CRs to the ED, or needs a second Architect instance, depends on volume.
- **Engagement windows:** one iteration by default keeps terms fresh but relocks often. Measure lock latency before lengthening it.
- **Sandbox hardening beyond v1:** restricted account + loopback-only API. Move to containers or Windows AppContainer if an agent ever executes generated code inside its sandbox (today, code runs only in CI).
