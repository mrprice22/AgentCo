# AgentCo — Knowledge Management & Collaboration

**Design document v0.1**
**Scope:** How the company learns and works together:
- a **knowledge base** built as a by-product of work
- a **traceability graph** linking knowledge to incidents, incidents to the changes that caused them, and problems to their fixes
- **lessons-learned control**, so a lesson counts as learned only once its action is verified
- **knowledge search before escalation or reporting**, which both deflects duplicate issues and keeps everyone on the same process
- how agents **meet**, **send mail**, and **share files and deliverables** without weakening need-to-know

Maps to ITIL 4 knowledge management, incident, problem, and continual improvement; follows the Knowledge-Centered Service (KCS) approach of capturing knowledge in the workflow rather than as a separate documentation task.

---

## 1. Why this is needed

The service management design (§12–§14) has runbooks, a known-error database, problem records, and an improvement register, but they aren't connected. Nothing links an incident to the change that caused it, or a resolved incident to the article that would prevent the next one. Nothing makes an agent look for an existing answer before escalating. And the only way agents interact is the job-and-message loop: there's no way to deliberate together, correspond, or hand over a document.

Two principles carry over from the rest of the design:
1. **Knowledge is shared, so it's governed.** Articles are canonical records under need-to-know, with an owner, a review, and a lifecycle, unlike an agent's private notes (data layer design §3.3).
2. **Collaboration is content, not command.** A meeting contribution, an email, or a file is material to work with (company directive D7). None of them changes state. Changing state still requires the Agent API verb that the role is permitted to call (agent API design §3).

---

## 2. The knowledge base

### 2.1 Article types

| Type | Answers | Typical source |
|---|---|---|
| **Procedure** | "How do we do X here?" (process alignment) | Scrum Master, Architect, owner |
| **Resolution** | "This symptom has this fix" | Incident resolution |
| **Known error** | "This defect exists; here's the workaround" | Problem management |
| **Decision explainer** | "Why is it done this way?" | Architecture and product decisions |
| **Lesson** | "What we learned and what changed because of it" | PIRs, retros, Inspect & Adapt |
| **Runbook** | "What the owner or driver does when X happens" | Service management §12 |
| **Reference** | Standards, conventions, glossaries | Architect, owner |

### 2.2 Schema

```yaml
article_id: kb-0142
type: resolution
title: "qwen36 emits invalid JSON when acceptance criteria contain nested quotes"
state: published            # draft → in_review → published → flagged → retired
version: 3
owner_role: developer
reviewers: [architect]
topics: ["kb.engineering.llm-output"]
data_class: internal
audience: [developer, tester, architect]
body:
  symptoms: "schema_invalid on implement_task when AC text contains \" …"
  environment: "qwen36@sha256:3f9a…, prompt developer@blob:8e1f…"
  cause: "Quote escaping lost in the model's output formatting"
  resolution: "Engine applies schema-repair step for quote escaping (chg_0107)"
links:                      # the traceability graph (§3)
  - { rel: resolves, target: inc_0031 }
  - { rel: resolves, target: inc_0038 }
  - { rel: implemented_by, target: chg_0107 }
reuse_count: 7
last_validated: "2026-10-11"
```

### 2.3 Storage and visibility

Articles are canonical records under `kb.*` topics, so the broker's need-to-know applies unchanged (data layer design §5). A security article under `kb.security.*` reaches only the roles cleared for security topics. Search results are filtered the same way: an agent never learns that an article it can't read exists.

### 2.4 Articles vs. private notes

| | Semantic notes | Knowledge articles |
|---|---|---|
| Owner | One agent | A role (with reviewers) |
| Visibility | That agent only | Need-to-know by topic |
| Review | None | Required before publishing |
| Purpose | "What I remember" | "What the company knows" |

**Promotion path:** at write-back (data layer design §6), an agent can call `kb.draft` for a note that would help other roles. The draft then goes through review like any other.

---

## 3. The traceability graph

Every record type already exists somewhere in the design. This section adds the **typed links** between them, stored as edges on the records themselves and queryable as one graph:

```
                  implemented_by                    caused_by
   knowledge ◀──────────────────── change ◀───────────────────────── incident
   article                          ▲   │ contained_in                   │
      ▲  ▲                          │   ▼                                 │ instance_of
      │  │ documented_as    fixed_by│  release                            ▼
      │  └───────────────────── problem ◀─────────────────────────────── (incidents)
      │ resolves
      └─────────────────────── incident
   lesson ── derived_from ──▶ incident | PIR | retro | meeting
   lesson ── actioned_by ──▶ change | enabler story;   captured_in ──▶ article
```

| Link | Meaning | Created by |
|---|---|---|
| `incident caused_by change` | This change introduced the defect | Deterministic correlation, confirmed per §3.1 |
| `incident resolves ← article` | This article resolved the incident | Required at incident closure (§4) |
| `incident instance_of problem` | Part of a recurring pattern | Problem detection rules (service management §13) |
| `problem documented_as article` | Known-error article | Problem record |
| `problem fixed_by change` | The permanent fix | Change record |
| `change contained_in release` | Release contents | Change coordinator (agent API design §5.3) |
| `change informed_by article` | Knowledge used in the change | CR author |
| `lesson derived_from / actioned_by / captured_in` | Lessons control (§6) | Lesson owner |

### 3.1 Finding the change that caused a defect

`caused_by` is the link that turns incident history into a change-quality signal, so it's established carefully:

1. **Deterministic candidates:** changes deployed in the window before the incident started, filtered by the configuration items the incident touches (change/config design §2 relationships).
2. **Evidence:** a revert or bisect in staging, where one is cheap.
3. **Agent analysis:** the Architect drafts a root-cause note from the incident trace (service management §5.3). It's a draft, not a verdict.
4. **Confirmation:** automatic when step 2 is conclusive. Otherwise the problem owner confirms, with the owner (the human) informed for Sev1/Sev2.

Confirmed `caused_by` links feed the DORA **change failure rate** directly, and let the change coordinator see, for any proposed change, the incident history of the components it touches.

---

## 4. Creating knowledge from work

Following KCS, knowledge is captured **as part of resolving things**, not afterwards:

- **At incident closure**, the engine requires one of two things before the incident can close: a link to an existing article that resolved it (a *reuse*), or a new article draft (a *capture*). This is a workflow guard (core engine design §5.2), not a reminder.
- **At problem closure**, the known-error article is updated with the permanent fix and the workaround is retired.
- **At a referral's answer** (for example, the PO clarifying acceptance criteria), the answering role may mark the answer as a *procedure* or *decision explainer* candidate, so the next agent with the same question finds it (§5).
- **Structure first:** resolution and known-error articles use the symptoms / environment / cause / resolution form, because that's what search matches on.
- **Reuse improves articles:** every reuse increments `reuse_count`. A reuser who finds an article wrong or incomplete **flags** it, which reopens review.
- **Retire deliberately:** an article unused for two PIs, or superseded, is retired with a link to its replacement, never deleted.

**RACI** (agent API design §4.4): the resolving role is **R** for authoring; the Scrum Master, as knowledge manager, is **A** for publishing; the Architect is **C** on technical accuracy. Articles that describe policy or change governance also need the owner's approval.

---

## 5. Search, deflection and process alignment

### 5.1 Agents search before they escalate

The `escalate` and `refer` verbs, and incident creation, require a knowledge search first:

- The gateway runs `kb.search` with the issue text and returns the top matches, filtered by need-to-know.
- The escalation call must include `kb_checked`: either the article IDs considered, or `none_applicable` with a one-line reason when a strong match was returned. An escalation without it is rejected as a schema failure.
- If a matching article resolves the issue, nothing is escalated. That's a **deflection**, counted per role.
- Escalations that happen *after* a KB miss are flagged as **article gaps**, and a weekly digest lists them as knowledge work for the knowledge manager.

This is a standing obligation in every engagement contract (agent API design §6.1).

### 5.2 The human searches too

When the owner files an incident, uses *Improve this page…*, or reports a problem in the console, matching articles are shown before submission. The search is deterministic (§5.4), so it runs in the console core with no LLM. In chat, Client Communications searches the knowledge base before answering, and cites articles the same way it cites metrics (console design §6.3).

### 5.3 Process alignment

Procedures are how the company keeps every role doing things the same way:
- **Required reading:** an engagement's scope of work lists the procedure articles its role must follow, such as Definition of Done, commit conventions, or the escalation reason guide. The job cycle's context assembly (core engine design §8.1) includes the relevant ones.
- **One source:** when a procedure changes, the new version supersedes the old (data layer design §7.2), so agents holding the old version in memory get corrected.

### 5.4 Search technology

Start with **SQLite FTS5 (BM25 ranking)**. It's deterministic, local, cheap, and good at the symptom-text matching resolutions need. Embedding search stays deferred until measured recall demands it, consistent with the data layer design's retrieval stance (§7.1 of that doc).

---

## 6. Lessons-learned control

A lesson is only *learned* when something changed and the change was verified. The control enforces that:

```yaml
lesson_id: les-0019
statement: "Decomposition exceeded max_relevant_files for migration stories 4 times in PI 3"
derived_from: [pir_0007, retro_it41]
category: process            # process | technical | tooling | governance | communication
owner_role: scrum_master
action: { kind: change_proposal, ref: chg_0122 }   # or enabler story, or article
verification: { metric: "role.architect.decomposition_rejections", expect: "≤ 1 per PI for 2 PIs" }
state: actioned              # captured → actioned → verified → closed   (or: rejected, with reason)
repeat_of: null
```

**Rules:**
- **Every PIR, retro, and Inspect & Adapt** produces zero or more lessons. An empty result is recorded as a deliberate outcome, not an omission.
- **No lesson closes without verification:** a metric condition over a stated window, or an explicit check result.
- **Repeat detection:** new lessons are matched against closed ones (the same FTS search). A match is flagged as a **repeat lesson**, meaning the earlier action didn't hold. It automatically opens a problem record.
- **Lessons become knowledge:** a closed lesson is captured as a *lesson* article, linked to its action, so later teams learn from it without repeating it.
- **One register:** the lessons register *is* the lessons view of the improvement register (service management design §14). Every lesson is an improvement entry, not a second list.

The PI report (service management design §11.2) lists open lessons, verification due dates, and repeat-lesson count.

---

## 7. Meetings

### 7.1 What a meeting is

A meeting is a work item of kind `meeting`. It's a structured, facilitated, budgeted multi-agent session. It isn't an open-ended conversation, which with LLMs tends to be expensive and inconclusive.

```yaml
meeting_id: mtg-0088
type: design_review          # standup | planning | design_review | incident_review | retro |
                             # change_review | knowledge_review
agenda:
  - { item: "Auth token storage for client-app-1", decision_owner: architect }
participants: [architect, developer, security_reviewer]
scribe: scrum_master
scope_topics: ["architecture.auth", "security.finding.auth"]
max_rounds: 3
budget: { tokens: 60000 }
outputs: [minutes, decisions, action_items, lessons, kb_drafts]
```

### 7.2 Facilitation is deterministic

The **engine** facilitates, not an agent. For each agenda item it runs rounds:
1. Each participant receives the agenda item, the scoped context, and the previous rounds' contributions, all filtered by need-to-know.
2. Each participant returns a structured `meeting.contribute`: position, proposals, questions, objections, and whether it agrees.
3. The meeting stops at consensus or at `max_rounds`. If it's unresolved, the item's **decision owner**, per RACI, decides within its authority or escalates or refers as usual.

Round limits and a token budget keep meetings from turning into endless chatter. Each turn is a job, scheduled like any other (core engine design §7).

### 7.3 Need-to-know in meetings

A meeting has a **topic scope**, and every invitee must be cleared for all of it. `meeting.request` fails for an invitee who isn't, and the requester sees which participant and which topic. Contributions are tagged with topics, and the broker rejects any that fall outside the meeting's scope. A meeting can't become a way to share information that the message path would have blocked.

### 7.4 Outputs

- **Minutes:** written by the scribe from the structured contributions, and shown with the agent-authored frame in the console.
- **Decisions:** published as decision records when within the decision owner's authority (data layer design §3.4).
- **Action items:** become work items through `item.create`, under the creator's permissions.
- **Lessons and knowledge drafts:** enter §6 and §4.

### 7.5 Ceremonies are meetings

Planning, retros, incident reviews, and change reviews (CAB preparation) become meetings of this form. The daily standup stays a cheap status aggregation by the Scrum Master: no deliberation needed, so it isn't worth multi-agent turns.

### 7.6 The owner in meetings

The owner can **observe** any meeting live or replayed (monitor design §5.7), since the console reads everything. The owner doesn't speak to agents directly (company directive D12, one voice). To contribute, the owner messages Client Communications, which can add the input to the agenda as a clearly labeled *client note*.

---

## 8. Mail

Internal mail is an addressed, threaded, asynchronous message: subject, body, to/cc (roles or role instances), attachments (file references, §9), related items, and priority. It runs on the message bus with a mail envelope.

- **Who can mail whom:** RACI relationships (consulted and informed parties on shared activities), or a shared work item (for example, everyone on the same story). The broker also checks need-to-know on the mail's topics, per recipient.
- **Mail doesn't command:** a request in a mail changes nothing. Work that needs doing becomes a work item. Received mail is content under D7, delivered to prompts as labeled untrusted material (governance design §7).
- **Limits:** per-engagement rate limits and a token budget, so agents can't mail each other into a feedback loop.
- **No external email:** agents never send mail outside the company. Messages meant for the owner are Client Communications' console messages (the conversation channel); anything leaving the company would be an `irreversible_external_action` (governance design §4).
- **The owner can read every mailbox** in the console, because the console is ACL-exempt (monitor design §1).

---

## 9. Files and deliverables

### 9.1 The document store

A versioned store for non-code artifacts: specs, designs, test plans, reports, release notes, runbooks, diagrams, datasets. Code stays in repositories (change/config design §8).

```yaml
file_id: fil-0310
title: "client-app-1 test plan, PI 4"
type: test_plan
owner_role: tester
product: client-app-1
related: [feature-12, story-88]
topics: ["story.*.acceptance_criteria"]
data_class: internal
state: accepted              # draft → in_review → accepted → delivered → superseded
versions: [{ v: 1, sha256: "…" }, { v: 2, sha256: "…" }]
shared_with: [developer, product_owner]
```

- Content is stored by hash, with metadata in the orchestrator's store, and included in backups (service management design §8).
- `file.put` runs the secret scanner (governance design §5.3) and assigns a data class from the file's topics.
- `file.share` grants access to roles permitted by RACI and need-to-know. There are no public or anonymous links.
- Files are content (D7). Nothing in a file is ever executed.

### 9.2 Deliverables

A **deliverable** is a file an engagement contract says a role owes (agent API design §6.1): a test plan per feature, release notes per release, a threat model per epic, a design doc per enabler.

- **Accepted by the RACI accountable party** (`file.accept`): the PO for test plans, the Architect for designs, the change coordinator's release notes by the owner as part of release approval.
- **Deliverables to the owner** (reports, release notes, the PI report, product documentation) appear on a **Deliverables** page in the console. The owner accepts or rejects them through the trusted confirmation dialog (console design §3.3), and a rejection note becomes feedback for the owning role.
- **Contract compliance:** missing or rejected deliverables count as engagement breaches, and appear in the owning role's dashboard.

---

## 10. Metrics

Registry entries (console design §7.1):

| Metric | Meaning |
|---|---|
| Deflection rate | Share of would-be escalations or incidents resolved by an existing article |
| Article gap rate | Escalations after a KB miss |
| Knowledge coverage | Share of closed incidents linked to an article |
| Reuse per article; stale articles | Health of the knowledge base |
| Time to publish | Draft → published |
| Change failure rate | Deployments with ≥ 1 confirmed `caused_by` incident (DORA) |
| Lesson closure rate; repeat-lesson rate | Whether lessons actually get learned |
| Meeting cost per decision; rounds to consensus | Whether meetings earn their tokens |
| Deliverable acceptance rate | By role and type |

---

## 11. Console integration

New presentation pages (console design §5): **Knowledge** (search, article view, and graph view of an article's links), **Lessons**, **Meetings** (live, scheduled, and minutes), **Mailboxes** (per role), and **Files & deliverables**. Accepting a deliverable and confirming a `caused_by` link for Sev1/Sev2 are console commands behind the trusted dialog. The trace view (monitor design §5.7) gains the graph's links, so an incident's page shows its causing change, its release, and the articles and lessons it produced.

---

## 12. Control mapping

| Framework | Reference | Section |
|---|---|---|
| ITIL 4 | Knowledge management | §2, §4, §5 |
| ITIL 4 | Incident / Problem management (linking, known errors) | §3, §4 |
| ITIL 4 | Continual improvement (lessons control) | §6 |
| ITIL 4 | Change enablement (change failure attribution) | §3.1 |
| 800-53 | IR-4 Incident handling (lessons learned incorporated) | §6 |
| 800-53 | AC-4 Information flow enforcement (meetings, mail, files) | §7.3, §8, §9 |
| SSDF | RV.3 Root-cause analysis | §3.1 |
| AI RMF | MANAGE 4 (post-deployment monitoring, incident learning) | §3, §6 |

---

## 13. Changes required to existing documents

| Document | Change |
|---|---|
| Service management design §12–§14 | Known-error DB = known-error articles; runbooks = runbook articles; improvement register gains the lessons view; incident closure guard (reuse or capture) |
| Core engine design §4.4, §10 | Work kinds `meeting`, `lesson`; ceremonies run as meetings; incident-closure guard in `workflow.yaml` |
| Data layer design §5.1 | Topics `kb.*`, `mail.*`, `meeting.*`, `file.*` with classes |
| Agent API design §3 | `kb.*`, `mail.*`, `meeting.*`, `file.*` verbs and `kb_checked` on escalation (listed there) |
| Change/config design §13 | Change failure rate computed from confirmed `caused_by` links |
| Console design §5 | Knowledge, Lessons, Meetings, Mailboxes, and Files & deliverables pages; deliverable acceptance command |
| Prompt templates | `meeting_contribution` mode; the `kb_checked` obligation; citing articles |

---

## 14. Open questions / follow-ups

- **Meeting value:** multi-agent deliberation may cost more than it adds for small local models. Measure *meeting cost per decision* against the same decisions made by a single decision owner, and keep meetings only where they win.
- **Caused-by confidence on small incidents:** deterministic correlation is weak when several changes ship together in one release. Smaller releases help; so does the change coordinator recording which configuration items each change touches.
- **Article quality from T0 authors:** resolution articles written by local models may be thin. Review catches some; the article-gap metric will show whether authoring should move to T2 for some article types.
- **Owner-facing knowledge:** whether procedures and decision explainers should also be readable as a human handbook in the console, curated separately from agent-facing knowledge.
