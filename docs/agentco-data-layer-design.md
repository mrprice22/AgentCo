# AgentCo — Data Layer & Memory Architecture

**Design document v0.2**
**Scope:** How individual agents store what they learn, how information moves between sandboxed agents, and how the system guarantees no single agent has a full picture of the project.

| Version | Changes |
|---|---|
| v0.1 | Original design: per-agent memory, canonical records, message envelope, need-to-know broker, write-back, retrieval and staleness |
| v0.2 | Integrates the later designs: the Agent API is the only I/O (agent API design); envelope v2 with a bus-stamped sender, topic-derived data class, and trace, baseline, and engagement fields; topic classes and new topics for change, engagement, knowledge, mail, meetings, files, and metrics; where each kind of record lives; encrypted stores; cross-agent conflicts resolved by referral. Fixes the "three tables" wording. |

---

## 1. Constraints this design is built around

1. **Agents run in sandboxes.** Each agent is an isolated OS process with no shared filesystem, no shared database connection, and no ability to inspect another agent's memory, prompt, or state directly (agent API design §2.1).
2. **The Agent API is the only I/O.** Everything a sandbox does is a call to the role-scoped Agent API: sending a message (`mail.send`, `record.publish`), reading its own memory (`notes.query`), receiving what the broker delivered (`record.get`), calling a model (`model.complete`). An agent's entire experience of "the project" is what those calls returned. If it wasn't told something, it doesn't know it; there is no backdoor read.
3. **No agent is omniscient.** This isn't just a side effect of sandboxing; it's a requirement. The orchestration processes (driver, engine, broker, router) and the monitor/console *do* have full visibility, for operational reasons and because the owner sits above the hierarchy. None of them is an agent, none has an LLM in its own path, and none answers questions using that visibility (monitor design §1). They only move messages, enforce policy (§5), and show the owner the truth.
4. **Memory is per-agent and durable.** An agent's context window is not its memory — it's a cache. What an agent "remembers" long-term is whatever it has chosen to write into its own persistent store, because the conversation that taught it that fact will eventually be evicted from context.

These constraints mean the data layer has to solve two problems that are usually conflated in single-agent systems: **where facts are stored** (§3) and **who is allowed to learn a given fact at all** (§5). Designing them together is the point of this document.

---

## 2. Architecture overview

```
┌───────────────────────────────────────────────────────────────────────┐
│                          ORCHESTRATION LAYER                            │
│                                                                         │
│  ┌──────────────────┐   ┌───────────────┐   ┌───────────────────┐     │
│  │ AGENT API GATEWAY │──▶│  message_bus   │──▶│  need-to-know      │     │
│  │ identity · run    │   │  (routes only) │   │  broker (ACL check)│     │
│  │ state · engagement│   └───────────────┘   └─────────┬─────────┘     │
│  │ · permission ·    │                                   │               │
│  │ scope · class     │──▶ engine (applies proposals) ──▶ records service │
│  └────────▲─────────┘                                   │               │
│           │ every call passes the gateway, then the broker before       │
│           │ delivery — a sender can't force-deliver                     │
└───────────┼───────────────────────────────────────────────┼─────────────┘
            │ loopback, per-sandbox credential               │
   ┌────────┴─────────┐   ┌──────────────────┐     ┌─────────▼────────┐
   │  Agent Sandbox A  │   │  Agent Sandbox B  │ ... │  Canonical Store │
   │ ┌──────────────┐ │   │ ┌──────────────┐ │     │  (topic-scoped,  │
   │ │ working mem   │ │   │ │ working mem   │ │     │  append-only,    │
   │ │ (context win) │ │   │ │ (context win) │ │     │  no raw access)  │
   │ └──────────────┘ │   │ └──────────────┘ │     └──────────────────┘
   └──────────────────┘   └──────────────────┘
      own episodic log + semantic notes live on the host, in that agent's
      encrypted store, reachable only through notes.* verbs (§3, §9)
```

Two things make this different from a typical "shared vector DB all agents query":

- **There is no store any agent can query except its own.** The "Canonical Store" isn't a shared knowledge base agents SELECT from. It's written by the **engine** when it applies an authorized proposal, such as a PO decision, an architecture record, a published article, or a locked engagement (core engine design §11.2). It's *distributed* to other agents only as messages, filtered by the broker. An agent never runs a query against it; it only ever receives what the broker decided to hand it.
- **The gateway and the broker sit between every send and every deliver.** Sandboxes can't message each other directly even if they wanted to: the Agent API on loopback is the only network destination a sandbox has (agent API design §2.1). Every call is checked by the gateway (identity, run state, engagement, permission, scope, data class), and every delivery passes the broker's ACL check before the recipient's inbox is written to.

---

## 3. Memory taxonomy

Every agent has one persistent store: a SQLite file, one per agent, **encrypted with SQLCipher** (§9). It holds **two tables**, the episodic log (§3.2) and semantic notes (§3.3). Working memory (§3.1) is the third *kind* of memory, but it isn't a table: it's the live context window. Shared records (§3.4) live outside any agent's store.

### 3.1 Working memory (ephemeral — not a table, just the live context window)

What's in the prompt right now. Bounded by the model's context length (32K for `qwen36`, 16K for `deepseek9b`/`deepseek`, larger for T2/T3), and by the task bounds that keep every job inside it (core engine design §6.2). Not persisted as such — anything here that matters must be written down before it's evicted (§6).

### 3.2 Episodic log (raw, append-only, agent-private)

```sql
CREATE TABLE episodic_log (
  msg_id         TEXT PRIMARY KEY,
  direction      TEXT CHECK(direction IN ('received','sent')),
  peer_role      TEXT,                -- who this came from / went to (bus-stamped, §4)
  task_id        TEXT,
  correlation_id TEXT,                -- trace id (§4)
  timestamp      TEXT,
  topic          TEXT,
  data_class     TEXT,                -- derived from topic (§5.1)
  raw_body       TEXT,                -- the message as sent/received, verbatim
  read_flag      INTEGER DEFAULT 0
);
```

This is the agent's literal inbox/outbox history — every message it has ever sent or received, unmodified. It's the ground truth for "what was I actually told," and it's what makes escalation packets (escalation design §4.3) reconstructable: an agent can always go back to the raw message rather than relying on its own summary of it.

Nothing here is visible to any other agent or used in the orchestrator's decisions. Only the owning agent (through `notes.*` verbs), compliance tooling (§8), and the owner's ACL-exempt console can read it.

### 3.3 Semantic notes (derived, agent-private, the actual "memory")

```sql
CREATE TABLE semantic_notes (
  note_id        TEXT PRIMARY KEY,
  topic          TEXT,                -- e.g. "story_88.acceptance_criteria"
  content        TEXT,                -- the agent's own distilled statement of the fact
  source_msg_ids TEXT,                -- FK-ish list into episodic_log, for traceability
  confidence     REAL,
  created_at     TEXT,
  superseded_by  TEXT NULL            -- note_id of whatever replaced this, if anything
);
```

This is what an agent actually *reasons from* on its next invocation — not the raw episodic log (too long, too unstructured to re-read every time), but its own compressed, topic-indexed understanding. Populated by a **write-back step** every agent runs at the end of a task (§6). Old notes aren't deleted when updated — they're marked `superseded_by`, so the agent's belief history is reconstructable (useful when an escalation later reveals the agent was working from stale information).

### 3.4 Canonical records (shared, but never directly queried by agents)

Held centrally, not in any sandbox:

```sql
CREATE TABLE canonical_records (
  record_id    TEXT PRIMARY KEY,
  topic        TEXT,                -- "vision", "architecture.auth", "decision.guest_checkout", "kb.…"
  version      INTEGER,
  content      TEXT,
  published_by TEXT,                -- role + tier (or "owner" / "driver") that wrote it
  data_class   TEXT,                -- derived from topic (§5.1)
  baseline_id  TEXT,                -- configuration baseline in force when published
  supersedes   TEXT NULL,           -- record_id this version replaces
  created_at   TEXT
);
```

This is where the vision, PO and architecture decisions, the owner's decision records (from `human_relay`), published knowledge articles, change records, and locked engagements live durably. It is the source of truth the *broker* consults; agents never touch this table. When a new record is published, the broker computes who needs to know (§5) and pushes a message to each of those agents; it never grants read access to the table itself.

### 3.5 Where each kind of record lives

The rest of the design adds several stores. Each has one writer and one purpose:

| Store | Answers | Written by | Agents read it? |
|---|---|---|---|
| Per-agent store: episodic log + semantic notes (§3.2–§3.3) | "What does this agent know?" | Bus delivery (episodic); the agent's write-back via `notes.write` (semantic) | Only its owner, via `notes.*` verbs |
| Canonical records (§3.4), including `kb.*`, `change.*`, `engagement.*` topics | "What's currently true or decided on topic X?" | The engine, applying authorized proposals | Only as broker-filtered deliveries |
| Engine event log (core engine design §11) | "What's the operational state of work, and how did it get here?" | The engine only | No |
| Audit log (governance design §12) | "Prove what happened, tamper-evidently" | The audit writer, consuming engine events and bus messages | No |
| File store (knowledge design §9) | Versioned non-code artifacts and deliverables | `file.put`, with a secret scan | Only via `file.get`, per share grants and need-to-know |
| Product repositories (change/config design §8) | Code | The driver only (sole committer) | Only via `code.read`, within engagement scope |

---

## 4. The message envelope (v2)

Every message carries enough metadata for the broker to route it and for the recipient to file it correctly on arrival:

```json
{
  "msg_id": "msg_6a19...",
  "from": {"sandbox_id": "sbx-07", "role": "product_owner", "instance": 1, "tier": "T2"},
  "to": {"role": "developer"},
  "task_id": "task_1284",
  "topic": "story_88.acceptance_criteria",
  "data_class": "internal",
  "correlation_id": "trc_5d02...",
  "causation_id": "evt_c09f...",
  "baseline_id": "bl_2026_09_27_a",
  "engagement_id": "eng-po-sbx07-it42",
  "body": "...",
  "supersedes": null,
  "timestamp": "..."
}
```

- **`from` is stamped by the gateway**, from the credential of the sandbox the call arrived on (agent API design §2.1). Any sender-supplied value is ignored. A T0 sandbox can't claim to be the Product Owner to gain its publish authority (governance design §6.1).
- **`topic`** is what semantic notes are indexed on and what the broker's ACL keys off (§5.1). It's the only routing field a sender provides.
- **`data_class`** is **derived from the topic** (§5.1), never declared by the sender. The router uses it for tier ceilings (escalation design §2.2).
- **`correlation_id`** is the trace ID: set when an idea or story is created and inherited by everything that follows. **`causation_id`** points to the event that directly caused this message (core engine design §11.1).
- **`baseline_id`** and **`engagement_id`** record the configuration and the locked engagement the sender was operating under (change/config design §3.4; agent API design §6).
- **`supersedes`** lets a later message explicitly invalidate an agent's existing semantic note on that topic, which is how stale knowledge gets corrected (§7.2).
- v0.1's sender-set `classification` field is **removed**. It duplicated the topic's ACL and invited senders to declare their own audience (§5.2).

Model calls aren't bus messages, but their records use the same trace fields plus `model_ci` and `prompt_ci`, the exact model and prompt versions used (§8).

---

## 5. Need-to-know enforcement

### 5.1 Topic ACL and topic classes

`config/need_to_know.yaml` maps topics (or topic prefixes) to the roles allowed to receive them, and to the data class each topic carries:

```yaml
topics:
  "vision":                         [product_owner, architect, executive_director, client_communications]
  "architecture.*":                 [architect, developer, security_reviewer]
  "story.*.acceptance_criteria":    [developer, tester, product_owner]
  "decision.*":                     [product_owner, architect, executive_director]
  "client.raw_communication":       [client_communications, executive_director]
  "security.finding.*":             [security_reviewer, architect, executive_director]
  "change.*":                       [change_coordinator, architect, security_reviewer, executive_director]
  "change.proposal.system":         [executive_director]
  "release.*":                      [change_coordinator, tester, product_owner, executive_director]
  "engagement.*":                   []                  # each sandbox receives only its own, via engagement.get
  "kb.engineering.*":               [developer, tester, architect, security_reviewer, scrum_master,
                                     knowledge_manager]
  "kb.process.*":                   [developer, tester, architect, security_reviewer, scrum_master,
                                     knowledge_manager,
                                     product_owner, change_coordinator, executive_director, client_communications]
  "kb.security.*":                  [security_reviewer, architect, executive_director, knowledge_manager]
  "mail.*":                         per-recipient      # addressed; broker checks the mail's topic tags per recipient
  "meeting.*":                      per-meeting        # participants must be cleared for the meeting's scope
  "file.*":                         per-share          # file.share grants within RACI and need-to-know
  "metrics.aggregate.*":            [client_communications, executive_director, scrum_master]
  "projection.work_items.*":        [client_communications, executive_director, product_owner, scrum_master]
default_deny: true

topic_classes:
  "vision":                   internal
  "story.*":                  internal
  "architecture.*":           internal
  "decision.*":               internal
  "decision.budget.*":        confidential
  "client.raw_communication": confidential
  "security.finding.*":       confidential
  "kb.security.*":            confidential
  "metrics.aggregate.*":      internal
default_class: confidential    # unlisted topics are treated as the most sensitive promptable class
```

`default_deny: true` means an unlisted topic reaches nobody automatically: a role has to be explicitly enumerated. This is the actual mechanism that keeps a developer agent from ever seeing, say, the raw text of what the human said to Client Communications, or a security finding that isn't relevant to the file it's touching. `default_class: confidential` is the matching safe default for data class: an unclassified topic can't reach a tier with a lower ceiling (governance design §5).

Client Communications gets metric aggregates and work-item projections so it can answer the owner's questions in chat with real numbers (console design §6.3). These are summaries; it still doesn't receive agent memory, raw message bodies, or security finding details beyond counts.

### 5.2 The sender names the topic; the broker does the rest

A sender provides a topic and a body. The broker derives everything else: the recipients (from the topic ACL), the data class (from `topic_classes`), and whether the sender may originate that topic at all (§5.3). If the sender lacks publish authority for the topic, the send is **rejected with an error** rather than silently narrowed, so misconfigurations surface immediately instead of silently under- or over-sharing.

### 5.3 Publish authority

Separate from *read* ACLs, `need_to_know.yaml` also (optionally) restricts who may *originate* a message on a given topic:

```yaml
publish_authority:
  "decision.*":              [product_owner, executive_director]
  "architecture.*":          [architect, executive_director]
  "change.proposal.system":  any                  # any role may propose; only the ED receives it
  "engagement.*":            [driver]             # locked by the driver from approved templates
  "kb.*":                    [knowledge_manager]  # other roles draft via kb.draft (knowledge design §4.1)
  "release.*":               [change_coordinator]
```

Topics without an explicit `publish_authority` entry default to "any role in the read ACL may publish" (useful for peer-to-peer topics like `story.*.acceptance_criteria`, where developer and tester both need to write and read). Topics *with* an entry restrict who can create the canonical version, which is what stops a T0 worker's guess from silently becoming project-wide "fact." Publishing to these topics also needs the matching Agent API verb in the role's generated permissions (agent API design §4). The ACL and the verb are two independent checks that must both pass.

### 5.4 Enforcement point

This all lives in two places that nothing can route around:
- the **Agent API gateway**, which binds identity to the sandbox's channel and checks the verb, engagement, and scope
- the **broker** on the bus's send path, which checks the topic ACL, publish authority, and class

The sandbox's only network capability is the Agent API on loopback. There's no second path to a peer, no shared disk, no shared cache. If either check rejects a call, the sandbox gets an error back and nothing is delivered. There's no fallback local write that could leak the content elsewhere, and every denial is audit-logged (agent API design §8).

---

## 6. Write-back: how a fact becomes memory

An agent doesn't automatically remember everything it's told — it has to convert working memory into a semantic note before context eviction, or the fact is gone. This happens at two points:

1. **On receipt of any message**, before it's added to the prompt for the current task, it's already durably written to `episodic_log` (raw) by the bus delivery itself. This part is automatic and not the agent's responsibility, precisely because we don't want memory durability to depend on the small model remembering to save things.
2. **At the end of every task**, the agent runs a mandatory write-back step: given its own episodic log entries touched during this task, produce 0–N `semantic_notes` rows summarizing what it should carry forward. This is a structured output submitted through the `notes.write` verb, not free text:

```json
{
  "notes": [
    {
      "topic": "story_88.acceptance_criteria",
      "content": "Guest checkout must not persist email after order completion.",
      "source_msg_ids": ["msg_6a19..."],
      "confidence": 0.9
    }
  ],
  "supersedes": [],
  "kb_drafts": []
}
```

This keeps the *raw* record (episodic log) and the *belief* record (semantic notes) separate on purpose: if an agent's summary later turns out to be wrong, both the audit trail (what was actually said) and the erroneous belief (what the agent concluded) are independently recoverable. That matters when an escalation needs to figure out *where* a misunderstanding entered the system.

**Promotion to shared knowledge.** A note that would help other roles can be proposed as a knowledge article via `kb_drafts` (the `kb.draft` verb). The draft goes through review before it's published under `kb.*` (knowledge design §2.4, §4). Private notes stay private; only reviewed articles become "what the company knows."

---

## 7. Retrieval and staleness

### 7.1 Retrieval

Before starting a task, the job cycle assembles the agent's context (core engine design §8.1) from:
- its own `semantic_notes`, filtered by `task_id` and relevant topic prefixes, never the full episodic log and never another agent's store
- canonical records the broker has already delivered to it
- the **procedure articles** its engagement lists as required reading, plus articles relevant to the task (knowledge design §5.3)

Each segment is labeled with its origin and data class (governance design §7).

Given the local hardware, note retrieval starts as simple structured filtering (topic prefix + recency): a SQLite query, not a vector DB. Knowledge-base search uses SQLite **FTS5 (BM25)**, which is deterministic and good at symptom-text matching (knowledge design §5.4). If retrieval quality becomes a problem as volume grows, a local embedding pass can be layered on top of the same tables without changing the write path (§10).

### 7.2 Staleness and correction

Because no agent has the full picture, two agents can end up with contradictory semantic notes about the same topic (e.g., the Architect changes a decision after the Developer already has the old one cached). Correction happens via the `supersedes` field (§4): when a canonical record changes, the broker doesn't just deliver the new fact; it delivers it as a `supersedes: <old record or topic>` message, and the recipient's write-back step must mark the corresponding old `semantic_notes` row as `superseded_by` rather than leaving two live beliefs about the same topic.

Two mechanisms catch what that misses:

- **Stale writes are rejected.** Work items carry a `version`; a proposal based on an outdated version is rejected by the engine and the job re-runs with fresh context (core engine design §14).
- **Stale actions surface as failures.** If an agent acts on a note that's since been superseded (it started before the correction arrived), that shows up as a normal validation failure downstream, e.g. tests failing against changed acceptance criteria.

A conflict that spans two roles' notes (a Developer and a Tester disagreeing because they hold different canonical versions) is **referred to the Architect** (core engine design §9) rather than escalated up either role's tier ladder. It's recorded as a possible problem pattern, never silently self-healed: a quiet auto-correction would hide a process gap worth noticing.

---

## 8. Audit trail vs. agent-queryable memory

The compliance audit log and this data layer are deliberately **not the same store**, and agents can't query the audit log:

- **Episodic/semantic memory** (§3.2, §3.3) is what an agent uses to *do its job*, scoped to what it's been told, per the need-to-know rules.
- **The audit log** is a separate append-only record of every message, Agent API call (allowed or denied), escalation, decision, and change across the *whole* system. The audit writer consumes engine events and bus messages and appends them to a **hash chain**, tagging each entry with the controls it evidences (governance design §12). Model-call entries carry `model_ci`, `prompt_ci`, and `baseline_id`, so any output can be traced to the exact prompt text, model hash, and configuration it ran under (change/config design §3.4). It's read by the owner's console and compliance tooling, never by an agent.

This separation is what actually enforces "no agent is aware of everything" at the infrastructure level rather than just by prompt instruction: even if an agent were prompted to "tell me everything you know," the most it could leak is its own inbox. The full-system view simply isn't reachable from inside any sandbox.

---

## 9. Storage/hosting notes for this environment

- All per-agent stores, canonical records, the engine event log, the audit log, and the file store live on the Windows host, written only by orchestration processes. Sandboxes never get a filesystem path to any `.db` file; they get only the Agent API.
- **Encryption at rest** (owner decision, 2026-09-27): the host check found the OS volume unencrypted and Secure Boot off, so Windows Device Encryption isn't available as configured. Every store is therefore **SQLCipher-encrypted**, and file-store blobs are encrypted, with the key held in Windows Credential Manager (DPAPI, tied to the owner's Windows login) (governance design §15). Backups are encrypted again client-side before leaving the host (service management design §8.2).
- **Sandboxes** (owner decision, 2026-09-27): one OS process per agent under a restricted local account, whose only network destination is the Agent API on loopback (agent API design §2.1). "Sandbox" means process-level isolation on the orchestration side; the llama.cpp-style endpoint is stateless per request and isolates nothing itself.
- Given the open, unauthenticated LAN (`10.42.0.0/24`), the gateway and broker are the actual security boundary for this system, not the network. The *data layer's* access control is enforced entirely in the orchestration processes, so a compromised or rogue sandbox has no path to another agent's memory or to the canonical store, even on a trusted-but-unauthenticated LAN. That's also why only the router holds the local inference endpoint's address (escalation design §6.5).

---

## 10. Open questions / follow-ups

- **Note pruning/compaction:** `semantic_notes` will grow without bound over a long project and needs a retention policy (e.g., collapse superseded chains older than N iterations into one archival summary). Tracked as `feat-p5-memory`.
- **Embedding-based retrieval** (§7.1) is deferred until volume demands it. Benchmark topic-prefix filtering and FTS5 first. Also tracked as `feat-p5-memory`.
- **Cross-agent conflict resolution ownership:** *resolved.* Conflicts spanning two roles' notes are referred to the Architect (§7.2; core engine design §9).
- **Sandbox implementation detail:** *resolved* by the owner (2026-09-27). Each agent runs in a sandbox whose only interface is the role-scoped Agent API; the v1 mechanism is one OS process per sandbox (agent API design §2).
