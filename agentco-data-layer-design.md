# AgentCo — Data Layer & Memory Architecture

**Design document v0.1**
**Scope:** How individual agents store what they learn, how information moves between sandboxed agents, and how the system guarantees no single agent (below Opus) has a full picture of the project.

---

## 1. Constraints this design is built around

1. **Agents run in sandboxes.** Each agent is an isolated process with no shared filesystem, no shared database connection, and no ability to inspect another agent's memory, prompt, or state directly.
2. **The message bus is the only I/O.** An agent's entire experience of "the project" is the set of messages it has sent and received. If it wasn't told something, it doesn't know it — there is no backdoor read.
3. **No agent is omniscient.** This isn't just a side effect of sandboxing — it's a requirement. Even the router/orchestrator process, which *does* have full visibility for operational reasons, is not itself an agent and never answers questions using that visibility; it only ever moves messages and enforces policy (§5).
4. **Memory is per-agent and durable.** An agent's context window is not its memory — it's a cache. What an agent "remembers" long-term is whatever it has chosen to write into its own persistent store, because the conversation that taught it that fact will eventually be evicted from context.

These constraints mean the data layer has to solve two problems that are usually conflated in single-agent systems: **where facts are stored** (§3) and **who is allowed to learn a given fact at all** (§5). Designing them together is the point of this document.

---

## 2. Architecture overview

```
┌───────────────────────────────────────────────────────────────────┐
│                         ORCHESTRATION LAYER                        │
│                                                                     │
│   ┌───────────────┐     ┌───────────────────┐     ┌─────────────┐ │
│   │  message_bus   │────▶│  need-to-know      │────▶│  records     │ │
│   │  (routes only) │     │  broker (ACL check)│     │  service     │ │
│   └───────┬───────┘     └───────────────────┘     └──────┬──────┘ │
│           │                                                │        │
│           │  every message passes through the broker      │        │
│           │  before delivery — sender can't force-deliver  │        │
└───────────┼────────────────────────────────────────────────┼───────┘
            │                                                 │
   ┌────────▼────────┐   ┌────────▼────────┐        ┌─────────▼────────┐
   │  Agent Sandbox A │   │  Agent Sandbox B │  ...   │  Canonical Store │
   │ ┌──────────────┐ │   │ ┌──────────────┐ │        │  (topic-scoped,  │
   │ │ working mem   │ │   │ │ working mem   │ │        │  append-only,    │
   │ │ (context win) │ │   │ │ (context win) │ │        │  no raw access) │
   │ ├──────────────┤ │   │ ├──────────────┤ │        └──────────────────┘
   │ │ episodic log  │ │   │ │ episodic log  │ │
   │ │ (own inbox/   │ │   │ │ (own inbox/   │ │
   │ │  outbox, raw) │ │   │ │  outbox, raw) │ │
   │ ├──────────────┤ │   │ ├──────────────┤ │
   │ │ semantic notes│ │   │ │ semantic notes│ │
   │ │ (own derived  │ │   │ │ (own derived  │ │
   │ │  summaries)   │ │   │ │  summaries)   │ │
   │ └──────────────┘ │   │ └──────────────┘ │
   └──────────────────┘   └──────────────────┘
```

Two things make this different from a typical "shared vector DB all agents query":

- **There is no store any agent can query except its own.** The "Canonical Store" on the right isn't a shared knowledge base agents SELECT from — it's written to by roles with publish authority (PO, Architect, Exec Director) and *distributed* to other agents only as messages, filtered by the broker. An agent never runs a query against it; it only ever receives what the broker decided to hand it.
- **The broker sits between every send and every deliver.** Sandboxes cannot message each other directly even if they wanted to — `message_bus` is the only network path out of a sandbox, and it always routes through the broker's ACL check before the recipient's inbox is written to.

---

## 3. Memory taxonomy

Every agent's persistent state (SQLite file, one per agent, one per sandbox) has exactly three tables, corresponding to three different *kinds* of memory that get treated differently:

### 3.1 Working memory (ephemeral — not a table, just the live context window)

What's in the prompt right now. Bounded by the model's context length (32K for `qwen36`, 16K for `deepseek9b`/`deepseek`, larger for T2/T3). Not persisted as such — anything here that matters must be written down before it's evicted (§6).

### 3.2 Episodic log (raw, append-only, agent-private)

```sql
CREATE TABLE episodic_log (
  msg_id       TEXT PRIMARY KEY,
  direction    TEXT CHECK(direction IN ('received','sent')),
  peer_role    TEXT,                -- who this came from / went to
  task_id      TEXT,
  timestamp    TEXT,
  classification TEXT,              -- see §5.2
  raw_body     TEXT,                -- the message as sent/received, verbatim
  read_flag    INTEGER DEFAULT 0
);
```

This is the agent's literal inbox/outbox history — every message it has ever sent or received, unmodified. It's the ground truth for "what was I actually told," and it's what makes escalation packets (from the escalation design doc) reconstructable: an agent can always go back to the raw message rather than relying on its own summary of it.

Nothing here is visible to any other agent or to the orchestrator's own decision-making — only to compliance tooling (§8) and to the owning agent itself.

### 3.3 Semantic notes (derived, agent-private, the actual "memory")

```sql
CREATE TABLE semantic_notes (
  note_id      TEXT PRIMARY KEY,
  topic        TEXT,                -- e.g. "story_88.acceptance_criteria"
  content      TEXT,                -- the agent's own distilled statement of the fact
  source_msg_ids TEXT,              -- FK-ish list into episodic_log, for traceability
  confidence   REAL,
  created_at   TEXT,
  superseded_by TEXT NULL           -- note_id of whatever replaced this, if anything
);
```

This is what an agent actually *reasons from* on its next invocation — not the raw episodic log (too long, too unstructured to re-read every time), but its own compressed, topic-indexed understanding. Populated by a **write-back step** every agent runs at the end of a task (§6). Old notes aren't deleted when updated — they're marked `superseded_by`, so the agent's belief history is reconstructable (useful when an escalation later reveals the agent was working from stale information).

### 3.4 Canonical records (shared, but never directly queried by agents)

Held centrally, not in any sandbox:

```sql
CREATE TABLE canonical_records (
  record_id    TEXT PRIMARY KEY,
  topic        TEXT,                -- "vision", "architecture.auth", "decision.guest_checkout"
  version      INTEGER,
  content      TEXT,
  published_by TEXT,                -- role + tier that wrote it
  visibility   TEXT,                -- topic-based ACL key, see §5.1
  created_at   TEXT
);
```

This is where PO decisions, the vision doc, architecture decisions, and human decision records (from `human_relay`) live durably. It is the source of truth the *broker* consults — agents never touch this table. When a new record is published, the broker computes who needs to know (§5) and pushes a message to each of those agents' inboxes; it does not grant read access to the table itself.

---

## 4. The message envelope (unchanged core, extended for memory)

Every message crossing the bus carries enough metadata for the recipient to file it correctly on arrival:

```json
{
  "msg_id": "msg_6a19...",
  "from": {"role": "product_owner", "tier": "T2"},
  "to": {"role": "developer", "tier": "T0"},
  "task_id": "task_1284",
  "topic": "story_88.acceptance_criteria",
  "classification": "need_to_know:developer,tester",
  "body": "...",
  "supersedes": null,
  "timestamp": "..."
}
```

- `topic` is what semantic_notes gets indexed on and what the broker's ACL keys off (§5.1).
- `classification` is set by the *sender*, but is not trusted blindly — the broker independently checks the sender's publish authority for that topic before delivering (§5.3). A T0 developer cannot mark something `need_to_know:everyone` and have it actually go everywhere.
- `supersedes` lets a later message explicitly invalidate an agent's existing semantic note on that topic, which is how stale knowledge gets corrected (§7).

---

## 5. Need-to-know enforcement

### 5.1 Topic ACL

`config/need_to_know.yaml` maps topics (or topic prefixes) to the roles allowed to receive them:

```yaml
topics:
  "vision":                    [product_owner, architect, executive_director, client_communications]
  "architecture.*":            [architect, developer, security_reviewer]
  "story.*.acceptance_criteria": [developer, tester]
  "decision.*":                 [product_owner, architect, executive_director]
  "client.raw_communication":   [client_communications, executive_director]
  "security.finding.*":         [security_reviewer, architect, executive_director]
default_deny: true
```

`default_deny: true` means an unlisted topic reaches nobody automatically — a role has to be explicitly enumerated. This is the actual mechanism that keeps a developer agent from ever seeing, say, the raw text of what the human said to Client Communications, or a security finding that isn't relevant to the file it's touching.

### 5.2 Message classification vs. topic ACL

`classification` on the envelope (§4) is a convenience the sender sets; `topic` is what's authoritative. If they disagree — e.g., a message tagged with a topic the sender has no publish authority over — the broker rejects the send and returns an error to the sender rather than silently narrowing it, so misconfigurations surface immediately instead of silently under- or over-sharing.

### 5.3 Publish authority

Separate from *read* ACLs, `need_to_know.yaml` also (optionally) restricts who may *originate* a message on a given topic:

```yaml
publish_authority:
  "decision.*": [product_owner, executive_director]
  "architecture.*": [architect, executive_director]
```

Topics without an explicit `publish_authority` entry default to "any role in the read ACL may publish" (useful for peer-to-peer topics like `story.*.acceptance_criteria` where developer and tester both need to write and read). Topics *with* an entry — decisions, architecture — restrict who can create the canonical version, which is what stops a T0 worker's guess from silently becoming project-wide "fact."

### 5.4 Enforcement point

This all lives in one place — `orchestration/message_bus.py`'s send path — specifically so it can't be bypassed by a sandbox escape or a misbehaving agent. The sandbox's only network capability is a call to `message_bus.send(envelope)`; there is no second path to a peer, no shared disk, no shared cache. If the broker rejects a send, the sandbox gets an error back and nothing is delivered — there's no fallback local write that could leak the content elsewhere.

---

## 6. Write-back: how a fact becomes memory

An agent doesn't automatically remember everything it's told — it has to convert working memory into a semantic note before context eviction, or the fact is gone. This happens at two points:

1. **On receipt of any message**, before it's added to the prompt for the current task, it's already durably written to `episodic_log` (raw) by the bus delivery itself — this part is automatic and not the agent's responsibility, precisely because we don't want memory durability to depend on the small model remembering to save things.
2. **At the end of every task**, the agent runs a mandatory write-back step: given its own episodic_log entries touched during this task, produce 0–N `semantic_notes` rows summarizing what it should carry forward. This is a structured output, not free text:

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
  "supersedes": []
}
```

This keeps the *raw* record (episodic_log) and the *belief* record (semantic_notes) separate on purpose: if an agent's summary later turns out to be wrong, both the audit trail (what was actually said) and the erroneous belief (what the agent concluded) are independently recoverable — which matters when an escalation needs to figure out *where* a misunderstanding entered the system.

---

## 7. Retrieval and staleness

### 7.1 Retrieval

Before starting a task, an agent's context is assembled from `semantic_notes` filtered by `task_id` / relevant `topic` prefixes — not the full episodic log, and never another agent's store. Given the local hardware, retrieval starts as simple structured filtering (topic prefix + recency), not embedding search — SQLite query, not a vector DB. If retrieval quality becomes a problem once note volume grows, a local embedding pass (using one of the existing local models or a small dedicated embedding model) can be layered on top of the same table without changing the write path.

### 7.2 Staleness and correction

Because no agent has the full picture, two agents can end up with contradictory semantic notes about the same topic (e.g., Architect changes a decision after Developer already has the old one cached). Correction happens via the `supersedes` field on new messages (§4): when a canonical record changes, the broker doesn't just deliver the new fact — it delivers it as a `supersedes: <old_msg_id or topic>` message, and the recipient's write-back step is required to mark the corresponding old `semantic_notes` row as `superseded_by` rather than leaving two live beliefs about the same topic.

If an agent acts on a note that's since been superseded elsewhere (race condition — it started a task before the correction arrived), that surfaces as a normal validation failure downstream (tests fail against changed acceptance criteria, etc.), which is exactly the kind of thing that should escalate per the escalation design rather than be silently self-healed — a quiet auto-correction would hide a process gap that's worth someone noticing.

---

## 8. Audit trail vs. agent-queryable memory

The compliance audit log (from the escalation design) and this data layer are deliberately **not the same store**, and agents cannot query the audit log:

- **Episodic/semantic memory** (§3.2, §3.3) is what an agent uses to *do its job* — scoped to what it's been told, per the need-to-know rules.
- **Audit log** is a separate, orchestrator-owned append-only record of every message, escalation, and decision across the *whole* system — used for compliance (NIST SSDF/AI RMF mapping) and debugging, read only by human-facing tooling, never by an agent.

This separation is what actually enforces "no agent is aware of everything" at the infrastructure level rather than just by prompt instruction: even if an agent were prompted to "tell me everything you know," the most it could leak is its own inbox — the full-system view simply isn't reachable from inside any sandbox.

---

## 9. Storage/hosting notes for this environment

- All per-agent SQLite files and the canonical_records store live on the Windows host, written by the orchestration process only — sandboxes never get a filesystem path to any `.db` file, only the `message_bus.send()` / task-input API.
- Given the open, unauthenticated LAN (`10.42.0.0/24`), the broker is the actual security boundary for this system, not the network — this reinforces the point in the escalation doc that the local inference endpoint should never be exposed beyond that subnet, and extends it: the *data layer's* access control is enforced entirely in the orchestration process, so a compromised or rogue sandbox has no path to another agent's memory or to the canonical store even on a trusted-but-unauthenticated LAN.
- Because T0 workers run against a local endpoint with no built-in session isolation, "sandbox" here should be understood as **process-level isolation on the orchestration side** (each agent's state and message handling is a separate process/context) rather than relying on the inference server itself to isolate anything — the llama.cpp-style endpoint is stateless per request regardless.

---

## 10. Open questions / follow-ups

- **Note pruning/compaction:** `semantic_notes` will grow unbounded over a long project; needs a retention policy (e.g., collapse superseded chains older than N sprints into a single archival summary) before this becomes a real system, not just a design.
- **Embedding-based retrieval** (§7.1) is deferred until note volume actually demands it — worth benchmarking topic-prefix filtering first against the local model set before adding a vector store.
- **Cross-agent conflict resolution ownership:** §7.2 says staleness surfaces as a task failure and escalates, but doesn't yet specify *which* role's escalation ladder handles a conflict that spans two roles' notes (e.g., Developer vs. Tester disagreement traced back to different canonical versions) — likely resolved by the Architect role, but not yet formalized.
- **Sandbox implementation detail** (containers vs. OS processes vs. separate Python interpreters) is left open — this document specifies the *contract* (no shared FS/DB, message-bus-only I/O) rather than the mechanism, since that's an implementation choice independent of the data model.
