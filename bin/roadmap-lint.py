#!/usr/bin/env python3
"""Validate roadmap.yaml against the AgentCo work model.

Usage:
    python bin/roadmap-lint.py [path/to/roadmap.yaml] [--summary]

Exit status is 1 when any error is found, 0 otherwise. Warnings never fail.
Requires PyYAML (pip install pyyaml).

The rules mirror docs/agentco-core-engine-design.md §4 (work model, Definition of
Ready) and the schema documented in the roadmap.yaml header.
"""
from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("roadmap-lint: PyYAML is required (pip install pyyaml)")

ID_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
MAX_TITLE_LEN = 100
KINDS = ("epic", "feature", "story", "task", "decision")
PARENT_KIND = {"feature": "epic", "story": "feature", "task": "story"}
NATURES = ("business", "enabler")
WSJF_KEYS = ("business_value", "time_criticality", "risk_reduction", "job_size")
CLOSED = ("done", "cancelled")


def state_vocab(meta: dict, kind: str) -> list[str]:
    states = meta.get("states", {})
    if kind == "epic":
        return states.get("epic", [])
    if kind == "decision":
        return states.get("decision", [])
    return states.get("work", [])


def find_cycles(deps: dict[str, list[str]]) -> list[list[str]]:
    cycles, color, stack = [], {}, []

    def visit(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in deps.get(node, []):
            if color.get(nxt) == 1:
                cycles.append(stack[stack.index(nxt):] + [nxt])
            elif color.get(nxt) is None:
                visit(nxt)
        stack.pop()
        color[node] = 2

    for node in deps:
        if color.get(node) is None:
            visit(node)
    return cycles


def lint(doc: dict) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    err, warn = errors.append, warnings.append

    for key in ("meta", "products", "groups", "items"):
        if key not in doc:
            err(f"missing top-level key '{key}'")
    if errors:
        return errors, warnings

    meta = doc["meta"] or {}
    products = {p.get("id") for p in doc["products"] or []}
    groups = {g.get("id") for g in doc["groups"] or []}
    phases = {p.get("id") for p in meta.get("phases", [])}
    items = doc["items"] or []

    ids: dict[str, dict] = {}
    for it in items:
        iid = it.get("id")
        if not iid:
            err(f"item without id: {it.get('title', '?')!r}")
            continue
        if not ID_RE.match(str(iid)):
            err(f"{iid}: id must be lowercase-hyphen")
        if iid in ids:
            err(f"{iid}: duplicate id")
        ids[iid] = it

    deps: dict[str, list[str]] = {}
    children: dict[str, list[dict]] = defaultdict(list)

    for iid, it in ids.items():
        kind = it.get("kind")
        state = it.get("state")
        where = f"{iid} ({kind})"

        for field in ("kind", "title", "product", "group", "state", "owner_role"):
            if it.get(field) in (None, ""):
                err(f"{where}: missing '{field}'")
        if kind not in KINDS:
            err(f"{where}: unknown kind")
            continue

        title = str(it.get("title", ""))
        if len(title) > MAX_TITLE_LEN:
            err(f"{where}: title is {len(title)} chars (max {MAX_TITLE_LEN})")

        if it.get("product") not in products:
            err(f"{where}: unknown product '{it.get('product')}'")
        if it.get("group") not in groups:
            err(f"{where}: unknown group '{it.get('group')}'")
        if "phase" in it and it["phase"] not in phases:
            err(f"{where}: unknown phase '{it['phase']}'")
        if state not in state_vocab(meta, kind):
            err(f"{where}: state '{state}' is not valid for kind '{kind}'")

        if kind != "decision" and it.get("nature") not in NATURES:
            err(f"{where}: nature must be one of {NATURES}")

        parent = it.get("parent")
        if kind in PARENT_KIND:
            if not parent:
                err(f"{where}: missing parent ({PARENT_KIND[kind]})")
            elif parent not in ids:
                err(f"{where}: parent '{parent}' does not exist")
            elif ids[parent].get("kind") != PARENT_KIND[kind]:
                err(f"{where}: parent '{parent}' is a {ids[parent].get('kind')}, "
                    f"expected {PARENT_KIND[kind]}")
            else:
                children[parent].append(it)
        elif parent and parent not in ids:
            err(f"{where}: parent '{parent}' does not exist")

        if kind in ("epic", "feature") and state not in CLOSED:
            wsjf = it.get("wsjf")
            if not isinstance(wsjf, dict):
                err(f"{where}: open {kind} needs wsjf inputs")
            else:
                for k in WSJF_KEYS:
                    v = wsjf.get(k)
                    if not isinstance(v, int) or v <= 0:
                        err(f"{where}: wsjf.{k} must be a positive integer")

        if kind in ("story", "task") and state not in ("draft", "cancelled"):
            ac = it.get("acceptance_criteria")
            if not ac:
                err(f"{where}: state '{state}' requires acceptance_criteria "
                    f"(Definition of Ready)")

        if kind == "decision":
            if not it.get("question"):
                err(f"{where}: decision needs a question")
            opts = it.get("options") or []
            opt_ids = [str(o.get("id")) for o in opts]
            if len(opt_ids) != len(set(opt_ids)):
                err(f"{where}: duplicate option ids")
            rec = it.get("recommended")
            if opts and rec is not None and str(rec) not in opt_ids:
                warn(f"{where}: recommended '{rec}' is not one of the option ids")
            if state == "done" and not it.get("resolution"):
                err(f"{where}: a decided item needs a resolution")

        if state == "done" and not it.get("date"):
            err(f"{where}: done items need a date")
        if state not in CLOSED and not it.get("source"):
            err(f"{where}: open items need a source (where the to-do was raised)")

        dep_list = it.get("depends_on") or []
        for dep in dep_list:
            if dep not in ids:
                err(f"{where}: depends_on '{dep}' does not exist")
            elif dep == iid:
                err(f"{where}: depends on itself")
        deps[iid] = [d for d in dep_list if d in ids]

    for cycle in find_cycles(deps):
        err("dependency cycle: " + " -> ".join(cycle))

    for pid, kids in children.items():
        parent = ids[pid]
        open_kids = [k["id"] for k in kids if k.get("state") not in CLOSED]
        if parent.get("state") == "done" and open_kids:
            warn(f"{pid}: done but has open children: {', '.join(open_kids)}")
        if parent.get("state") in ("funnel", "draft") and any(
                k.get("state") in ("in_progress", "in_review", "in_test", "done")
                for k in kids):
            warn(f"{pid}: still '{parent.get('state')}' while children are underway")

    return errors, warnings


def summary(doc: dict) -> str:
    items = doc.get("items") or []
    by_kind = Counter(i.get("kind") for i in items)
    by_state = Counter((i.get("kind"), i.get("state")) for i in items)
    lines = ["items: " + ", ".join(f"{k}={by_kind[k]}" for k in KINDS if by_kind[k])]
    for kind in KINDS:
        states = sorted((s, n) for (k, s), n in by_state.items() if k == kind)
        if states:
            lines.append(f"  {kind:8s} " + ", ".join(f"{s}={n}" for s, n in states))
    waiting = [i for i in items if i.get("kind") == "decision"
               and i.get("state") == "awaiting_human"]
    if waiting:
        lines.append(f"decisions awaiting the owner ({len(waiting)}):")
        lines += [f"  - {i['id']}: {i['title']}" for i in waiting]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    path = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "roadmap.yaml"
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    errors, warnings = lint(doc)
    for w in warnings:
        print(f"warning: {w}")
    for e in errors:
        print(f"error: {e}")
    if "--summary" in argv:
        print(summary(doc))
    if errors:
        print(f"{path.name}: {len(errors)} error(s), {len(warnings)} warning(s)")
        return 1
    print(f"{path.name} OK ({len(doc['items'])} items, {len(warnings)} warning(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
