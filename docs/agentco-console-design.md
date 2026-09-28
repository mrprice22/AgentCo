# AgentCo — Console (Human Interface) Design

**Design document v0.1**
**Scope:** The web interface the human uses to see, steer, and talk to the company. It covers:
- what it reuses from the NWN roadmap editor (`nwn-homers-lotr/bin/roadmap-editor.py`), in both structure and visual style
- a conversational interface to Claude
- pages for running the simulated company, portfolio views, and metrics
- dashboards that each agent role maintains for itself
- how the console becomes **a product AgentCo itself builds and improves**, without the company ever being able to change what the human is told or what the human approves

This refines the monitor/driver design (v0.2). That document defines *what* the monitor must show and do. This one defines *how the interface is built, who builds it, and how it's kept honest.*

---

## 1. Three requirements, one tension

1. **Adopt a proven interface.** The NWN roadmap editor is a working, used, and repeatedly refined workspace UI. AgentCo should start from it rather than from a blank page.
2. **Talk to Claude directly.** Chat with the company's Claude Opus tier, in the console, about anything: ideas, status, decisions, metrics.
3. **The company improves its own console.** Improvements to the human interface are product work, delivered through the same backlog, gates, and releases as client work.

The tension is in the third requirement. The console is where the human **approves** things (system changes, production deployments, risk acceptances) and where the human **learns the truth** about the company (costs, failures, decisions made without them). If agents can freely rewrite the console, they can, even by accident, change what the human sees or what a click approves. That would undo the self-modification rule (change/config design §6) through the back door.

The design resolves it by splitting the console in two (§3):
- a small **trusted core** that only the human changes
- a large **presentation layer** that the company builds and owns, which can *show* anything but can *do* nothing without the core asking the human first

---

## 2. What we reuse from the NWN roadmap editor

The roadmap editor has already solved many of the problems AgentCo's console will face: a long single-user workspace, capability-gated controls, agent-written changes needing human review, and off-machine access. AgentCo adopts the patterns and the visual language, not the code structure (§2.3).

### 2.1 Patterns adopted

| NWN pattern | Where it lives there | AgentCo use |
|---|---|---|
| **Workspace shell:** navigator of one-link-per-line sections + closable in-app tabs, tab identity by key, panes *detached* not destroyed (unsaved edits survive tab switches) | `roadmap-editor.py` workspace shell | The console shell, as is (§5) |
| **Navigator filter box, collapsible-to-top-strip, resizable, persisted width** | Same | As is |
| **Server-side navigation history** (last 50 things opened, per account) | `auth.sqlite3` `navigation_history` | As is |
| **Capabilities as a data table** (`CAPS` / `ROLES`), not `if role ==` branches; **fail-closed** `caps-unknown` body class until the server confirms who you are | `roadmap_auth.py` | Console roles (§4) |
| **The server enforces; hiding a button is courtesy** ("Never treat `CAN()` as the security boundary") | Page script | Same rule; plus the presentation sandbox (§3.3) |
| **A decision is never a side effect.** Merit is paid only by the *Award merit* button, never by a status move | Award merit button | Approvals, deployments, and risk acceptances are explicit inbox actions, never side effects of a board drag or a chat message (§6.4) |
| **Disabled buttons keep their label and explain themselves on hover** | Pipeline buttons | Every gated action says *why* it's unavailable (eval regression, missing evidence, needs re-auth) |
| **Risk tier assigned by the recipe, never by asking a model** (`auto` / `review` / `hold`) | `bin/llm` ledger | Same principle as change classes (change/config design §4); the console's own changes are classified by what they touch (§8.3) |
| **LLM Changes review panel:** grouped by risk, worst first, Approve / Edit / Revert per change, before/after in a ledger | `review_api.py` | Change queue and release review for console releases (§8.4) |
| **Anti-clobber guard:** content hash of what you loaded; a save against a changed baseline is blocked with *Reload latest* / *Force* | Editor save path | Editing personal dashboards and settings proposals |
| **Anonymous `public` role gets a *different document*, not a filtered one** | `/api/public-data` | Any future read-only viewer role gets a redacted *projection*, never the full payload with things hidden in the browser (§4) |
| **Deep links open tabs** (`/#idea-<id>`) | Delegated click handler + `hashchange` | `/#story-88`, `/#esc-9f2a`, `/#chg-0042`, `/#trace-5d02`, and links inside chat replies |
| **Off-machine access through a Cloudflare Tunnel with login**, never an open port | `roadmap.homerslotr.com` | Remote and mobile inbox access (resolves monitor design §13's mobile question the same way) |
| **Queues as worklists** (UAT Queue grouped by who can clear it, "only mine") | UAT / Toolset queues | The inbox and the per-role work queues |
| **Separate monitor document for a second screen** | `/monitor` | The live feed and service view can be opened as standalone second-screen pages |

### 2.2 Visual language

The console uses the roadmap editor's design tokens **verbatim** so the two tools feel like one family, then adds AgentCo-specific tokens:

```css
:root {
  /* From nwn roadmap-editor.py — unchanged */
  --bg:#1e2127; --panel:#272b33; --ink:#e6e6e6; --mut:#9aa3af;
  --line:#3a3f4a; --accent:#6ea8fe; --warn:#e6b800; --err:#ff6b6b; --ok:#5cd6a0;

  /* AgentCo additions */
  --t0:#56d4dd;  --t1:#8fb3ff;  --t2:#d2a8ff;  --t3:#f0c96a;  --human:#7ee2a8;  /* tier colors */
  --mode-degraded:#d29922;  --mode-down:#ff7b72;  --mode-paused:#9aa3af;       /* service mode */
  --agent-authored:#b8a8f0;   /* border for any text an agent wrote (§7.4) */
  --simulated:#79c0ff;        /* border for any simulated figure */
  --provisional:#f0c96a;      /* provisional decisions */
}
```

It keeps the component vocabulary too: `system-ui` 14px/1.45, 6px radii, `.badge` for state, `.chip` for attributes, a **colored left-edge bar** on rows and cards (NWN uses it for deploy environment; AgentCo uses it for **tier** on work items and **service impact** on incidents), `#banner.ok/.bad/.warn`, sticky action bars, and modals. Dark is the default, as in NWN. Because every color is a token, a light theme can be added later by redefining tokens alone.

### 2.3 What we deliberately don't reuse

| NWN choice | Why AgentCo differs |
|---|---|
| **One 10,400-line file** with the page embedded as a Python string | AgentCo's agents work in bounded tasks: at most 3 files, ~200 new lines, ~12K tokens of context (core engine design §6.2). A monolith can't be decomposed into T0-sized work. The presentation layer is built as **small modules, one page per module, each under ~300 lines** (§9.2). This is the single most important structural difference, and it follows directly from requirement 3. |
| **YAML file as the database**, written by the editor | The engine's event log is the system of record (core engine design §11); the console never writes a store (monitor design §2) |
| `http.server` from the standard library | The console core needs WebSockets (live feed, inbox, banner). FastAPI, as the monitor design already specifies. |
| Publish = commit + push from the UI | Console releases go through the product delivery pipeline and the human's *Adopt* action (§8.4) |

**Reuse mechanics:** the shell CSS and JS are **vendored** into AgentCo (`console/core/shell/`) with an `UPSTREAM.md` recording the source repo and commit (`nwn-homers-lotr@32732964f53`). There's no live dependency between the repos: they have different governance, and AgentCo's copy becomes a configuration item under its own change control.

---

## 3. Architecture: trusted core, sandboxed presentation

### 3.1 The split

```
┌──────────────────────────── browser: console origin (core) ─────────────────────────────┐
│ ┌───────────────────────── CORE SHELL (human-approved code only) ──────────────────────┐ │
│ │ service-mode banner · navigator frame · tab bar · who-bar · start/pause               │ │
│ │ PROTECTED PAGES: Inbox · Decision log · Approvals · Changes & baselines · Audit ·     │ │
│ │                  Compliance · Company scorecard · Settings · Users                    │ │
│ │ TRUSTED CONFIRMATION DIALOG (the only UI that can send a write command)               │ │
│ └──────────────┬──────────────────────────────────────────────────────▲────────────────┘ │
│                │ postMessage: read results, navigation                 │ postMessage:     │
│                ▼                                                        │ read requests,   │
│ ┌──────────── PRESENTATION FRAME (agent-built product) ────────────────┴──────────────┐ │
│ │ <iframe sandbox="allow-scripts">  — separate origin, no cookies, CSP connect-src none │ │
│ │ Boards · Portfolio · Metrics & role dashboards · Chat · Traces · Forecasts · ...      │ │
│ └───────────────────────────────────────────────────────────────────────────────────────┘ │
└───────────────────────────────────────────┬─────────────────────────────────────────────┘
                                            │ session cookie (core origin only)
                                   ┌────────▼────────┐
                                   │  CONSOLE CORE   │  read API (allow-listed projections,
                                   │  API (FastAPI)  │  metric registry) · command API
                                   └────────┬────────┘  (fixed verbs, re-auth where required)
                                            │ commands
                                         DRIVER / ENGINE (monitor design §4)
```

| | Trusted core | Presentation layer |
|---|---|---|
| **Contains** | Auth, sessions, roles; the read API; the fixed command API; the metric registry (§7.1); the shell frame; protected pages; the trusted confirmation dialog; CSP and sandbox policy | Every other page, layout, widget, chart, dashboard spec, chat UI, style refinement |
| **Built by** | The human (with Claude Code, as the NWN editor was) | **AgentCo**, as product work (§8) |
| **Change class** | System-normal: human approval + eval gate (change/config design §4.2) | Product change: standard when gates pass (§8.3) |
| **Can read** | Everything (monitor is ACL-exempt, monitor design §1) | Only what the core's read API returns to it |
| **Can write** | Sends commands after human confirmation | **Nothing directly.** It can only *request* a command, which the core puts in front of the human (§3.3) |

### 3.2 Why a sandboxed frame

Presentation code is written by agents and runs inside the human's authenticated browser session. Without isolation, a bug or a successful prompt injection anywhere upstream could produce UI code that quietly calls `approve_change`. The NWN editor's rule, "the server enforces; hiding a button is courtesy," protects against a *user* pressing buttons they shouldn't. It doesn't protect against the *page itself* being adversarial. The sandbox does:

- The presentation frame is served from a **separate origin** (a second localhost port, or subdomain behind the tunnel) with `sandbox="allow-scripts"` and **no** `allow-same-origin`. It can't read the core's session cookie, can't call the core API directly, and can't touch the parent DOM.
- Its CSP is `default-src 'self'; connect-src 'none'; frame-ancestors <core origin>`. It **can't make network requests at all**, so console data can't be exfiltrated, and it can't load external scripts.
- It talks to the core shell only by `postMessage`. The shell **allow-lists** read requests (named projections and metric queries, with parameters validated) and returns data. It never forwards arbitrary requests.

### 3.3 Writes go through the trusted confirmation dialog

When presentation code wants something done (the human clicked *Approve* on a card in an agent-built page), it posts a **command request**: `{verb, target, reason_text}`. The core shell then:

1. Renders its **own** confirmation dialog, core code in the core origin, showing the verb and target in core-controlled wording, fetched fresh from the read API. It doesn't reuse the presentation's description.
2. Requires the human's click, plus re-authentication for high-impact verbs (monitor design §10.2).
3. Sends the command with session, CSRF token, and a single-use nonce minted by the core for that dialog.

So agent-built UI can make an action *convenient*, but it can't make one *happen*. A presentation page that requests `approve_change` in a loop just produces a series of core dialogs the human declines, and each request is logged as a presentation-origin anomaly.

### 3.4 Protected pages

Some pages are where the human decides things or checks the company's honesty. They're core code, and the presentation layer can link to them but can't replace, restyle beyond tokens, or hide them:

- **Service-mode banner** (always visible in the shell frame)
- **Inbox** and **Decision log** (resolved without you)
- **Approvals:** system changes, production deployments, risk acceptances
- **Pre-approval rules:** the standard-change catalog, with its backtest preview (change/config design §4.3)
- **Change approval meetings:** the owner's seat, where agenda items are approved with buttons (change/config design §5.1)
- **Changes & baselines**, **Audit log**, **Compliance**
- **Company scorecard:** the canonical metrics the human uses to judge the company (§7.2)
- **Settings**, **Users**, the Start / Pause control

Protected pages still use the shared tokens and shell, so they look like the rest of the console. They just change on a different schedule, by a different author.

---

## 4. Roles and capabilities

Same model as `roadmap_auth.py`: capabilities as a data table, roles as sets of capabilities, fail-closed rendering, server-side enforcement.

| Capability | Covers |
|---|---|
| `view` | All read views (the full, ACL-exempt monitor document) |
| `chat` | Conversations with Client Communications (§6) |
| `answer` | Answering escalation-decision inbox items |
| `approve_deploy` | Production deployment approvals |
| `approve_change` | System change approvals (re-auth) |
| `accept_risk` | Risk acceptances and renewals (re-auth) |
| `adopt_console` | Adopting a console preview release / reverting (§8.4) (re-auth) |
| `run_control` | Start / Pause the business simulation; pause a role or tier |
| `resume_revert` | Resume after an automatic Sev1 pause, revert baseline (re-auth) |
| `what_if` | Running simulation scenarios |
| `personal_dashboards` | Creating and editing the account's own dashboards (§7.5) |
| `audit_view` | Audit log and compliance evidence |
| `users` | Managing accounts (shell-only creation, as in NWN: `bin/console-users.py`) |

| Role | Has |
|---|---|
| `owner` | Everything. The human. |
| `observer` | `view`, `what_if`, `personal_dashboards`. For a trusted second person watching the company. |
| `auditor` | `audit_view` plus a **redacted projection** of the rest (no raw client communication), served as a *different document*, per the NWN `public` lesson |

v0.1 has one real user. The table exists so that adding a second one is a data change rather than a rewrite, which is what NWN learned when its editor went from "LAN-only, no auth" to "internet-reachable, several users."

**Sign-in (owner decision, 2026-09-27):** admin-role credential sign-in, the same pattern as the NWN server's `roadmap_auth.py`: accounts created from a shell on the host, password plus session cookie, capabilities by role, and the same login in front of the Cloudflare Tunnel for remote access. Re-authentication for high-impact verbs still applies.

---

## 5. Page map

The navigator follows NWN's shape: sections of one-link-per-line entries, filterable, with history. ● = protected core page; ○ = presentation (agent-built).

| Section | Pages |
|---|---|
| **Inbox** | ● Inbox · ● Decision log · ● Approvals · ● Change approval meetings · ○ My chat threads |
| **Company** | ○ Overview (home) · ○ Org chart & agents · ○ Iteration board · ○ Backlog (board / list) · ○ PI & roadmap · ○ Work queues by role |
| **Portfolio** | ○ Portfolio overview · ○ Portfolio Kanban · ○ Products (one per product, including the Console) · ○ Budgets & capacity allocation |
| **Claude** | ○ Talk to the company (chat) · ○ Discuss… (item-scoped chats) · ○ Conversation history |
| **Metrics** | ● Company scorecard · ○ Flow metrics · ○ DORA · ○ Cost & capacity · ○ Service levels · ○ Forecasts & what-if · ○ Role dashboards (one per role, agent-maintained, §7.3) · ○ My dashboards |
| **Operations** | ○ Live feed · ○ Traces & replay · ○ Incidents & problems · ○ Agent health · ● Changes & baselines · ○ Releases |
| **Compliance** | ● Controls & evidence · ● SSDF gates · ● Risk acceptances · ● Model inventory · ● Directive metrics |
| **Manage** | ● Settings · ● Pre-approval rules · ● Users · ● Notifications |
| **Console** | ○ Improve this page… (feedback) · ○ Console roadmap · ● Preview & adopt |

### 5.1 Key presentation pages

- **Overview (home):** answers "what needs me, and is everything OK?" in one screen. It shows the inbox count by urgency, service mode, spend today against guardrail, per-product RAG, what finished since the last visit, and decisions made without you since the last visit. The console's own north-star metric (§8.5) is measured against this page.
- **Org chart & agents:** the org chart rendered live, with each role's instances, tier colors, current jobs, health, and a link to that role's dashboard. It's the "company" view: who works here and what they're doing right now.
- **Portfolio overview:** one row per product (client products and the Console itself) with RAG, forecast P50/P85, budget burn against guardrail, capacity share, open decisions, and active epics.
- **Portfolio Kanban:** epics across products through the SAFe portfolio states (Funnel → Reviewing → Analyzing → Ready → Implementing → Done), each card carrying its Lean business case and WSJF. Details come in the SAFe/PMO design.
- **Budgets & capacity allocation:** how paid budget and local capacity are split across products (§8.2), with actual against allocated.

### 5.2 The bootstrap backlog: `roadmap.yaml`

Until the engine exists, the backlog lives in [`roadmap.yaml`](../roadmap.yaml) at the repo root, in the NWN `roadmap.yaml` tradition: stable IDs, groups, `notes`/`impl_notes`, and `date`/`commit` on shipped items. Its schema *is* the engine's work model (core engine design §4): epics, features, stories, tasks, and decision items, using the engine's state names. So the console's first page can be built before the engine (roadmap item `feat-ca-roadmap-board`):
- the Backlog board uses `meta.board_lanes`
- the Portfolio Kanban uses `meta.portfolio_lanes`
- decision items render like inbox entries

Build phase 0 imports the file into the engine as seed events (`feat-p0-seed-import`). After that, the engine is the system of record and `roadmap.yaml` becomes a generated export. `bin/roadmap-lint.py` validates the file (structure, references, states per kind, Definition of Ready, dependency cycles) and runs in the AgentCo repo's CI.

---

## 6. Talking to Claude

### 6.1 Who you're talking to

The chat is a conversation with **Client Communications**, the company's Claude Opus (T3) voice. That keeps company directive **D12** ("one voice to the client") and the escalation design's rule that only Opus talks to the human, with no exceptions. When a question needs the Executive Director (strategy, tradeoffs), Client Comms consults them and relays the answer. The human always has one counterpart.

The monitor design (§11) said a natural-language feature must be an agent, not a shortcut inside the monitor. This satisfies that: the chat UI is presentation code that posts messages; every reply is produced by an agent job in a sandbox, under the directive, need-to-know, and audit logging.

### 6.2 What the chat is for

| Intent | What happens |
|---|---|
| **Commission work** ("add dark mode to the product," "build me a CLI for X") | Captured as intake → Exec Dir/PO, exactly like the intake gateway. The reply links the created items. |
| **Ask about status** ("why is feature 4 red?") | Client Comms answers using the **metric query tool** (§6.3) and the item projections handed to it. The reply embeds live metric widgets, not typed numbers. |
| **Discuss a decision** ("what happens if I pick option b?") | Opened from the inbox's *Discuss…* button: a chat thread scoped to that `human_request`, with its packet, impact, and simulation results in context. |
| **Give feedback** ("this page is confusing") | Becomes a Console product backlog item (§8.1) |
| **Ask about the company** ("which role escalates most, and why?") | Metric tool + role dashboards; answer cites metric IDs |

### 6.3 The metric query tool

Client Comms gains a new job type, `converse`, with one tool: `query_metric(metric_id, filters, window)`. It runs against the **metric registry** (§7.1), a deterministic query engine with no LLM. The tool returns data. The chat UI renders the result as a real chart or stat tile *from the query result*, with the metric's definition on hover.

The reply text may discuss the numbers but can't be the source of them. A reply that states a figure no query produced gets a visible "unverified" chip. That's detected by comparing numbers in the text with the numbers in the job's query results. This is company directive **D2** (be honest) and the information-integrity control (governance design §10.2), turned into interface.

**Need-to-know:** Client Comms' scope is extended to **metric aggregates and work-item projections**, which are Internal-class summaries. It still doesn't get agent-private memory, raw bus message bodies, or security finding details beyond counts. For those, the reply links to the relevant core page, which the human can open with their own ACL-exempt access.

### 6.4 Chat is not a command channel

Typing "approve change 42" or "deploy it" in chat **never** approves or deploys anything. Chat text is content (company directive D7) and has no authority, even though the human wrote it. That's deliberate: a command channel made of free text is a command channel anything can inject into. Client Comms answers with a **link to the approval**, and the approval happens through the trusted confirmation dialog (§3.3). This carries over the NWN principle that a decision is never a side effect (the Award merit button): an approval is always its own deliberate act.

### 6.5 Conversation mechanics

- **Latency:** the driver sends a templated "received" acknowledgment immediately (service management design §2.3), and the UI shows *Client Communications is drafting…*. The T3 OLA is ≤ 3 min p90; typical replies are faster.
- **Threads:** each thread is a work item (`conversation`) with a correlation ID. Threads are listed, searchable, and linked from the items they touched.
- **Cost:** every thread shows its cost. There's no separate chat budget (owner decision, 2026-09-27). The owner controls spend with the business simulation's **Start / Pause** button (monitor design §4.2). While paused, no model is called; chat messages queue with an acknowledgment and are answered after Start.
- **During `t3_down`:** the chat shows the service mode and queues messages, which are answered on recovery. It never falls back to a lower-tier model (service management design §4.3).
- **Rendering:** reply text is rendered through a sanitizing allow-list (the same idea as NWN's rich-text whitelist for `notes`): basic formatting, internal deep links (`/#…`), and metric widgets only. There's no raw HTML and no external links unless allow-listed. Agent-authored content is framed with the `--agent-authored` border.

### 6.6 The owner's composer

Chat and meeting messages from the owner are writes, and the presentation layer is agent-built, so the text box itself belongs to the core. The **composer** is a core-owned input rendered in the shell, outside the presentation frame, and used for chat, meeting contributions, and feedback notes. Presentation pages can open it pre-addressed (to a thread, a meeting, or an item) but can't fill in or submit text; only the owner's keystrokes reach it. Those words carry no authority (§6.4), but a forged "client note" could still mislead agents, so agent-built UI must not be able to write one.

---

## 7. Metrics and dashboards

### 7.1 The metric registry (trusted core)

Every number the console shows comes from a **registered metric**: a named, versioned, deterministic query over engine projections, the audit log, or the cost ledger.

```yaml
# console/core/metrics/registry.yaml (excerpt)
- id: flow.time.story
  name: Story flow time
  unit: hours
  definition: "Wall-clock time from story entering 'ready' to 'done', p50 and p85, per iteration."
  source: engine.projections.work_items
  owner: human                # canonical
  version: 3
- id: role.developer.first_pass_rate
  name: Developer first-pass success
  unit: ratio
  definition: "Share of implement_task jobs reaching 'applied' with no retry, escalation, or rework."
  source: engine.events
  owner: human
  version: 1
- id: role.tester.escaped_defects
  name: Escaped defects
  unit: count
  definition: "Defects found after a story reached 'done', attributed to the story's test task."
  source: engine.events
  owner: agent:tester         # agent-defined — shown with a chip until promoted (§7.3)
  version: 1
```

- **Canonical metrics** (`owner: human`) are system configuration items. Changing an existing metric's definition is a system change, because redefining a metric can silently change what "good" means. Every SLI in the service management design, the DORA and flow metrics, cost, and forecast accuracy are canonical.
- **Agent-defined metrics** (§7.3) are added as product changes, carry an "agent-defined" chip everywhere they appear, and can be **promoted** to canonical by the human.
- **Every widget shows its metric's ID, definition, and version on hover**, so no number appears in the console without a stated meaning.

### 7.2 Company scorecard (protected)

The page the human uses to judge the company, so it's core code and agents can't change it. It shows a fixed set of canonical metrics:
- delivery against forecast (SLA-7) and flow time
- DORA metrics
- cost per story point and spend against guardrails
- human requests per PI (SLA-6) and decision reversal rate
- incident count by severity
- SLO error budgets
- directive-principle violation rates (company directive §4)

It includes the unflattering ones by design. A presentation page can show more, but the scorecard guarantees there's always one page where the company can't choose what the human sees.

### 7.3 Role-maintained dashboards

Each agent role **owns and maintains its own dashboard**: what it thinks best shows how well it's doing its job. This makes performance self-reflection part of each role's work instead of something only imposed from outside.

**Mechanics:**
- A dashboard is a **declarative spec** in the Console product repo: `dashboards/roles/<role>.yaml`. It's a grid of widgets (stat tile, line, bar, table, burndown, cumulative flow), each referencing **registry metric IDs** plus filters.
- A spec **can't compute anything**; it only arranges registered metrics. A role can make its dashboard emphasize, compare, and annotate, but it can't produce a number.
- New job type per role: `maintain_dashboard`, scheduled at each iteration retro (core engine design §10). The role reviews its metrics and may propose spec changes, new agent-defined metrics (with definitions), and a short **commentary**.
- **Commentary** is agent-authored text, shown with the `--agent-authored` border, and must cite the metric IDs it discusses. Uncited figures get the "unverified" chip (§6.3).
- Spec and metric changes flow as Console product changes (§8.3).

**Starting points** (each role is free to evolve its own):

| Role | Suggested dashboard metrics |
|---|---|
| Developer | First-pass success, rework cycles per task, scope violations, tokens per task, tier escalation rate |
| Tester | First-pass verdict rate, escaped defects, flaky-test rate, test-task duration |
| Security Reviewer | Findings by CWE, gate latency (OLA), human-overturned verdicts (false positive/negative proxy), fast-path escalations |
| Architect | Decomposition rejections by bound, referral turnaround, architecture decisions later superseded |
| Product Owner | Stories meeting Definition of Ready, WSJF coverage, referral answer time, backlog age |
| Scrum Master | Flow time, flow load vs. WIP limits, blocked time by cause, ceremony on-time rate |
| Executive Director | Decisions without human, human-overturned decisions, human requests per PI, time to decide |
| Client Communications | Chat response time, report on-time rate, "unclear question" flags from the human, unverified-figure rate |

**Guardrails against self-flattery:** a role's dashboard sits beside that role's slice of the **company scorecard**, which it can't edit. If a role's own dashboard and the canonical metrics disagree about how it's doing, both are on screen at once.

### 7.4 Presentation integrity rules

These apply to every presentation page and are enforced by the Console's CI gates (§8.3) and by the core's rendering of data:

1. Numbers come only from registry queries. No hardcoded or agent-typed figures in any rendered view.
2. Agent-authored text always has the `--agent-authored` frame; simulated figures always have the `--simulated` frame; provisional decisions always have the `--provisional` frame.
3. The service-mode banner and core navigator sections can't be covered: they're outside the frame.
4. Every widget exposes its metric definition on hover.
5. No page can suppress an inbox item or an incident from its own lists. If a page filters, the filter is visible and one click away from "show all," the same lesson as NWN's always-visible filter bar.

### 7.5 Personal dashboards

The human can build their own dashboards directly in the console: drag widgets, pick metrics, save. They're **personal settings**, stored per account like NWN's navigation history, so they aren't company product and aren't change-controlled. They use the anti-clobber hash guard for concurrent edits. If the human wants one shared as a company page, *"Propose as product page"* files it as a Console backlog item.

---

## 8. The console as a product

### 8.1 Product registration

The Console is registered in the portfolio as **Product 0: AgentCo Console**. It's internal, with the human as its Business Owner:
- **Product vision** (written by the human and Exec Dir, published to the Console's `vision` topic): *"The human can understand and steer the company in five minutes a day, trusting that what the console shows is true."*
- **Repository:** `agentco-console` (presentation layer, dashboard specs, agent-defined metrics), separate from the AgentCo repo that holds the trusted core (change/config design §8.1).
- **Backlog sources:** the *Improve this page…* button on every page, which captures page ID, view state, and optional note, then goes through intake → Client Comms → PO; chat feedback (§6.2); roles' dashboard proposals (§7.3); the PO's own UX backlog; and console problem records (for example, a slow page).

### 8.2 Capacity allocation

A company that can improve its own tools will happily spend all its time doing so. The portfolio therefore allocates capacity explicitly, as a Lean budget guardrail set by the human in `portfolio.yaml`:

```yaml
products:
  console:
    kind: internal
    capacity_share: { paid_budget: 0.15, local_gpu: 0.20 }   # ceilings, not targets
    wsjf_floor: 5            # console items must still earn their place against client work
  client-app-1:
    kind: client
    capacity_share: { paid_budget: 0.85, local_gpu: 0.80 }
```

The engine scheduler enforces per-product shares. This is the multi-product extension flagged in core engine design §19. When there's no client work, the console can use idle capacity above its share, but never pushes out ready client work.

### 8.3 Delivery pipeline for console changes

Console changes use the normal product pipeline (change/config design §8), with UI-specific gates added to CI:

| Gate | Checks |
|---|---|
| Sandbox conformance | No network calls, no external scripts, no `eval`, no attempt to reach `parent` beyond `postMessage`; CSP violations in headless run = fail |
| Integrity rules (§7.4) | Lint + render tests: every number traces to a registry query; agent-authored/simulated/provisional frames present |
| Accessibility | Automated axe checks; keyboard navigation of new controls; contrast of any new color against `--bg` |
| Visual regression | Headless screenshots of changed pages, before/after, attached to the change record |
| Size budget | Per-page module ≤ ~300 lines; bundle size budget per page |
| Protected-page immutability | The build contains no file under `core/` and no override of protected routes |

**Change classes:** a presentation change that passes every gate is a **standard change**, merged without approval. A change that adds a dashboard **metric definition** is normal-minor (Architect review). Anything touching the core is not console product work at all; it's a **system change proposal** to the human (change/config design §6).

### 8.4 Preview, adopt, revert

The human's live console is production for this product, and production changes need the human (governance design §4). Built to keep that lightweight:

1. Every merged console change deploys automatically to the **preview** channel.
2. The human sees *"Console preview available: 4 changes"* in the core's **Preview & adopt** page, with each change's screenshots and description. It reuses the NWN LLM Changes panel pattern: grouped, before/after, per-item detail.
3. **Try preview** switches the presentation frame to the preview release *for this session only*. The core shell is unaffected, so protected pages and the banner are identical in both.
4. **Adopt** promotes preview to production (re-auth, `adopt_console`). **Revert** returns to any previous console release in one click.
5. **Reject** with a note sends the note to the Console backlog as feedback.

Batching is the human's choice: adopt after each change, weekly at PI review, or whenever.

### 8.5 Measuring the console

The Console product measures itself from the human's own use of it. Usage data is stored locally, Internal-class, and not sent anywhere:

| Metric | Meaning |
|---|---|
| **Time to understanding** (north star) | Median seconds from opening the console to either the first inbox action or closing it, on days without incidents |
| Inbox time-to-answer | Median time from opening an inbox item to answering it |
| Discuss rate | Share of inbox items where the human opened *Discuss…* before answering. High means the framing isn't clear enough (feeds Client Comms' dashboard). |
| Feedback volume and theme | *Improve this page* submissions per page |
| Adoption rate | Share of preview releases adopted vs. rejected or reverted |
| Page performance | p90 render time per page |

These metrics are canonical (human-owned) because they judge the product that shows the human everything else.

### 8.6 Bootstrapping

The company can't build the console it needs in order to run. So:

1. **Phase A (human + Claude Code):** extract the NWN shell into `console/core/shell/`; build the trusted core (auth, read and command APIs, confirmation dialog, protected pages, metric registry with canonical metrics) and a **minimal presentation** layer (boards, overview, chat). This lines up with core engine build phase 3.
2. **Phase B (handoff):** the presentation layer moves into the `agentco-console` repo and becomes Product 0. From here, the company owns it.
3. **Phase C (company-driven):** role dashboards, portfolio views, and everything else in §5 marked ○ are built by AgentCo as product work, prioritized by WSJF against client work within the capacity share.

---

## 9. Implementation notes

### 9.1 Core

- **Server:** FastAPI (monitor design §11), serving the core origin (shell, protected pages, APIs) and the presentation origin (static assets only) on separate ports: default `:8700` core and `:8701` presentation, both bound to localhost.
- **Remote access:** a Cloudflare Tunnel exposing the core origin, with the presentation origin as a second hostname. This mirrors `roadmap.homerslotr.com`. Remote sessions follow the same auth and re-auth rules.
- **Accounts:** created only from a shell on the host (`bin/console-users.py`), as in NWN.
- **Audit:** every command, login, failed login, confirmation shown/declined, and presentation-origin anomaly is logged. Navigation history stays out of the audit log, for the same reason NWN keeps it out (it would bury the writes the log exists for).

### 9.2 Presentation

- **No build step:** plain ES modules loaded by the frame, consistent with NWN's build-light approach and monitor design §11.
- **Structure for bounded tasks:**
  ```
  agentco-console/
    shell-bridge/        # the postMessage client (read requests, command requests) — tiny, stable
    components/          # badge, chip, card, lane, table, stat-tile, chart wrappers (each < 200 lines)
    pages/<page>.js      # one page per module (< 300 lines)
    dashboards/roles/<role>.yaml
    dashboards/pages/<page>.yaml
    metrics/agent-defined.yaml
    styles/tokens.css    # imports core tokens; adds nothing that overrides them
    tests/               # render, integrity, a11y, visual regression
  ```
- **Charts:** one small vendored charting library (for example uPlot, or Chart.js if its footprint is acceptable), wrapped by `components/`, so pages never call it directly and the library can be swapped as one change.
- **Filter state:** one object per view, persisted locally, with bars addressed by `data-f` attributes rather than IDs. That's NWN's fix for keeping duplicate bars in sync by construction.

---

## 10. Security summary

| Threat | Control |
|---|---|
| Agent-built UI performs actions on the human's behalf | Sandboxed frame; no cookies; commands only via core confirmation dialog + nonce (§3.2, §3.3) |
| Agent-built UI hides bad news | Protected pages and scorecard; banner outside the frame; integrity rules and CI gates (§3.4, §7) |
| Data exfiltration by presentation code | `connect-src 'none'`; separate origin; no external scripts |
| XSS via agent-authored text (chat, commentary) | Sanitizing allow-list renderer; no raw HTML (§6.5) |
| Commands injected via chat | Chat is not a command channel (§6.4) |
| Redefining a metric to look better | Canonical metrics are system configuration items; agent-defined metrics chipped until promoted (§7.1) |
| Stolen session | Localhost default; tunnel + login for remote; re-auth for high-impact verbs (monitor design §10) |

---

## 11. Changes required to existing documents

| Document | Change |
|---|---|
| Monitor design §5, §11 | Page catalog splits into protected core pages and presentation pages; the natural-language feature is Client Comms' `converse` job type with the metric query tool, not a new role |
| Core engine design §3.1, §4.4, §7, §19 | `converse` and `maintain_dashboard` job types; `conversation` work kind; per-product capacity shares (multi-product portfolio); Console registered as Product 0 |
| Governance design §5, §6.2 | Client Comms need-to-know extended to metric aggregates and work-item projections; console usage data classified Internal; `tools_allowed: [query_metric]` for Client Comms |
| Change/config design §2, §8 | Console core = system CI; `agentco-console` repo = product; UI-specific CI gates; preview/adopt/revert channel |
| Service management design §2.3 | Chat reply OLA; console page-performance SLO; *Improve this page* as intake |
| Company directive | New principle D13: our own interface is product (applied in directive v1.1) |
| Client Communications prompt | `converse` mode; metric-citation rule; "chat is not a command channel" handling |

---

## 12. Open questions / follow-ups

- **Is a UX role needed?** UI design quality from T0 local models may be weak. Options: route `implement_ui_task` to T2 by default, add a `ux_designer` role (T2) that produces page specs and HTML mockups the human reviews in preview, or rely on the human's *Reject with note* loop. Measure adoption and reject rates first (§8.5).
- **Sandbox friction:** a `postMessage` bridge adds a layer every page must go through. If it slows development too much, the fallback is a same-origin presentation with a strict Trusted Types policy. That's weaker and would need its own risk acceptance.
- **Chart library choice** and whether the charting wrappers belong in the trusted core, so metric rendering is identical everywhere, or in presentation.
- **Upstreaming:** improvements the company makes to the shared shell style could flow back to the NWN editor. That's a manual, human-driven sync (the repos stay independent, §2.3).
