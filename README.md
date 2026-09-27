# AgentCo

An LLM-staffed software "company": role-based agents (Developer, Tester, Architect, Product Owner, Executive Director, etc.) run in isolated sandboxes, communicate only over a brokered message bus, and escalate work up a cost-ordered ladder of models. Only the top tier (Claude Opus) may contact the human.

> **Status:** design phase (v0.1). This repo currently contains design documents and agent prompt templates; no implementation yet.

## Design documents

| Document | Covers |
|---|---|
| [`agentco-escalation-design.md`](agentco-escalation-design.md) | Model tiers (T0 local → T3 Claude Opus → human), role→tier assignment, escalation triggers and packet format, routing and local-GPU contention |
| [`agentco-data-layer-design.md`](agentco-data-layer-design.md) | Per-agent memory (episodic log, semantic notes), canonical records, need-to-know broker and topic ACLs |
| [`agentco-monitor-driver-design.md`](agentco-monitor-driver-design.md) | The non-agent driver/monitor process: sandbox supervision, ceremony scheduling, dashboards, and the human decision loop |

Suggested reading order: escalation → data layer → monitor/driver.

## Agent prompts

[`agentPrompts/`](agentPrompts/) holds one Jinja template per role. Each defines the role's tier, what it can see, its boundaries, escalation triggers, and a strict JSON output contract plus write-back notes.

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
