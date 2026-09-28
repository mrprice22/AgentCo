# AgentCo

An LLM-staffed software "company": role-based agents (Developer, Tester, Architect, Product Owner, Executive Director, etc.) run in isolated sandboxes, communicate only over a brokered message bus, and escalate work up a cost-ordered ladder of models. Only the top tier (Claude Opus) may contact the human.

**Mission:** Turn the client's idea into working, secure software they can trust — spending their money and their attention only where it truly matters.

> **Status:** design phase (v0.1). This repo currently contains design documents and agent prompt templates; no implementation yet.

## Design documents

| Document | Covers |
|---|---|
| [`agentco-company-directive.md`](docs/agentco-company-directive.md) | Mission, vision, and the thirteen operating principles every agent receives; what enforces each; precedence; how the directive is delivered, tested, and changed |
| [`agentco-core-engine-design.md`](docs/agentco-core-engine-design.md) | The deterministic engine that runs the company: org and work models, workflow state machines, decomposition and task bounds, scheduling, the job cycle, event sourcing/replay, and simulation mode for forecasting |
| [`agentco-escalation-design.md`](docs/agentco-escalation-design.md) | Model tiers (T0 local → T3 Claude Opus → human), role→tier assignment, escalation triggers and packet format, routing and local-GPU contention |
| [`agentco-data-layer-design.md`](docs/agentco-data-layer-design.md) | Per-agent memory (episodic log, semantic notes), canonical records, need-to-know broker and topic ACLs |
| [`agentco-monitor-driver-design.md`](docs/agentco-monitor-driver-design.md) | The non-agent driver/monitor process (v0.2): hosts the engine, supervises sandboxes, enforces baselines, sole committer and CI, operational alerts and backups; dashboards, inbox, traces/replay, and the authenticated human decision loop |
| [`agentco-console-design.md`](docs/agentco-console-design.md) | The human interface: NWN roadmap-editor shell and style reused, trusted core vs. sandboxed agent-built presentation, chat with Claude (Client Comms), metric registry, role-maintained dashboards, portfolio views, and the console as Product 0 |
| [`agentco-agent-api-policy-design.md`](docs/agentco-agent-api-policy-design.md) | The role-scoped Agent API (the only interface a sandbox has), the RACI matrix that generates every permission, the change and release path to production, and the policy manager: locked engagements (policy, contract, scope of work) with no model calls before the lock |
| [`agentco-knowledge-collaboration-design.md`](docs/agentco-knowledge-collaboration-design.md) | Knowledge base built from work (KCS), the incident ↔ change ↔ knowledge traceability graph, lessons-learned control, search-before-escalate deflection, and agent meetings, mail, files, and deliverables |
| [`agentco-governance-policy.md`](docs/agentco-governance-policy.md) | Accountability and RACI, policy set, risk tolerance, data classification and egress, prompt-injection controls, model/supplier governance, NIST SSDF / AI RMF / 800-53 mapping |
| [`agentco-change-config-mgmt-design.md`](docs/agentco-change-config-mgmt-design.md) | Configuration items and baselines, change types and authority, the self-modification rule, prompt/model eval gates, delivery pipeline, release and rollback |
| [`agentco-service-management-design.md`](docs/agentco-service-management-design.md) | SLAs/OLAs/SLOs and error budgets, unanswered human requests, degraded modes and T3 outage, incident/problem management, alerting, capacity, backup/recovery, supplier operations, vulnerability remediation, status reporting |

Suggested reading order: company directive → core engine → escalation → data layer → monitor/driver → console → agent API & policy → knowledge & collaboration → governance → change/config → service management.

Planned: SAFe/PMO delivery (roles, PI planning, WSJF, RAID log, flow and DORA metrics). Tracked as `feat-safe-pmo-doc` in the roadmap.

## Roadmap

[`roadmap.yaml`](roadmap.yaml) is the backlog: every outstanding change, open question, build phase, and owner decision from the design docs, as epics → features → stories plus decision items, in the engine's work-model schema (see console design §5.2). Validate after editing:

```
pip install pyyaml
python bin/roadmap-lint.py --summary
```

## Agent prompts

[`agentPrompts/`](agentPrompts/) holds one Jinja template per role. Every template begins by including [`_company_directive.jinja`](agentPrompts/_company_directive.jinja), the company-wide mission and thirteen principles. Each defines the role's tier, what it can see, its boundaries, escalation triggers, and a strict JSON output contract plus write-back notes.

| Role | Default tier |
|---|---|
| `developer` | T0 |
| `tester` | T0 |
| `security_reviewer` | T0 (fast-path escalation to T2) |
| `scrum_master` | T0/T1 |
| `architect` | T1 |
| `product_owner` | T2 |
| `executive_director` | T3 (may contact the human) |
| `client_communications` | T3 (may contact the human) |
| `change_coordinator` | T0; T2 for release notes (template planned) |
| `knowledge_manager` | T0; T2 for writing or merging articles (template planned) |
