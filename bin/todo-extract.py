#!/usr/bin/env python3
"""Extract tasks from a printed Microsoft To Do list and check them against roadmap.yaml.

Usage:
    python bin/todo-extract.py <to-do.pdf> [--roadmap roadmap.yaml] [--json] [--text]

Prints one row per task and sub-step: its page, its parent task, and the
roadmap item that best matches it (or NO MATCH). This is a report for the
todo-intake skill, not a gate: exit status is 0 unless the PDF can't be read.

--text prints the extracted page text instead, for reading lines the parser
splits badly. In the printout a task's wrapped text sometimes lands above its
checkbox, so a fragment can end up on the neighbouring entry; read the PDF
text before acting on a NO MATCH.

Requires pdftotext (bundled with Git for Windows; poppler-utils elsewhere) and
PyYAML (pip install pyyaml).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    sys.exit("todo-extract: PyYAML is required (pip install pyyaml)")

TASK = "K"          # task heading glyph in the To Do printout
STEP = "\ufffd"     # sub-step checkbox glyph (decodes as the replacement character)
NOISE_RE = re.compile(r"^(\d+ of \d+|My Day.*|Printed with Microsoft To Do.*|\d+/\d+)$")
URL_RE = re.compile(r"https?://\S+")
WORD_RE = re.compile(r"[a-z0-9']{3,}")
STOP = set("the and for with that this are can from into have has was were will not "
           "but any all its their they them each every when what who how out per via".split())
MATCH_FLOOR = 0.34


def pdf_pages(pdf: Path) -> list[str]:
    exe = shutil.which("pdftotext")
    if not exe:
        sys.exit("todo-extract: pdftotext not found. It ships with Git for Windows "
                 "(C:\\Program Files\\Git\\mingw64\\bin) or with poppler-utils; put it on PATH.")
    out = subprocess.run([exe, "-raw", str(pdf), "-"], capture_output=True, check=True).stdout
    return out.decode("utf-8", errors="replace").split("\f")


def parse(pages: list[str]) -> tuple[dict, list[dict]]:
    header: dict = {}
    entries: list[dict] = []
    task = None
    for pno, page in enumerate(pages, start=1):
        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        if not lines:
            continue
        first = lines.pop(0)  # "<list name> <weekday, month day, year>"
        if not header:
            m = re.match(r"^(.*?)\s+(\w+day, .*\d{4})$", first)
            header = {"list": m.group(1), "printed": m.group(2)} if m else {"list": first, "printed": None}
        current = None
        for ln in lines:
            if NOISE_RE.match(ln):
                continue
            if ln == TASK or ln.startswith(TASK + " "):
                text = ln[len(TASK):].strip()
                current = {"page": pno, "kind": "task", "task": None, "text": text}
                entries.append(current)
                task = current
                continue
            if ln.startswith(STEP):
                for part in [p.strip() for p in ln.split(STEP)][1:]:
                    current = {"page": pno, "kind": "step",
                               "task": task["text"] if task else None, "text": part}
                    entries.append(current)
                continue
            if current is None:  # untitled lines before any marker: a list-level note
                current = {"page": pno, "kind": "note", "task": None, "text": ln}
                entries.append(current)
            else:  # wrapped continuation of the previous entry
                current["text"] = (current["text"] + " " + ln).strip()
                if current["kind"] == "task":
                    task = current
    return header, [e for e in entries if e["text"]]


def tokens(text: str) -> set[str]:
    return {w for w in WORD_RE.findall(text.lower()) if w not in STOP}


def item_text(item: dict) -> str:
    parts = [item.get(k) for k in ("title", "description", "notes", "question")]
    for k in ("source", "acceptance_criteria"):
        v = item.get(k)
        parts.extend(v if isinstance(v, list) else [v])
    return " ".join(str(p) for p in parts if p)


def best_match(entry: dict, items: list[tuple[str, str, set[str]]]) -> tuple[str | None, float]:
    words = tokens(entry["text"])
    urls = URL_RE.findall(entry["text"])
    best, score = None, 0.0
    for iid, text, itoks in items:
        s = 1.0 if any(u.rstrip("/.,") in text for u in urls) else (
            len(words & itoks) / len(words) if words else 0.0)
        if s > score:
            best, score = iid, s
    return (best, score) if score >= MATCH_FLOOR else (None, score)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pdf", type=Path)
    ap.add_argument("--roadmap", type=Path, default=Path(__file__).resolve().parent.parent / "roadmap.yaml")
    ap.add_argument("--json", action="store_true", help="print JSON instead of a markdown table")
    ap.add_argument("--text", action="store_true", help="print the extracted page text and exit")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Windows consoles default to cp1252

    if not args.pdf.is_file():
        sys.exit(f"todo-extract: {args.pdf} not found")
    pages = pdf_pages(args.pdf)
    if args.text:
        for pno, page in enumerate(pages, start=1):
            if page.strip():
                print(f"--- page {pno} ---\n{page.strip()}\n")
        return 0

    header, entries = parse(pages)
    doc = yaml.safe_load(args.roadmap.read_text(encoding="utf-8")) or {}
    items = [(it["id"], item_text(it), tokens(item_text(it)))
             for it in doc.get("items") or [] if it.get("id")]
    for e in entries:
        e["match"], score = best_match(e, items)
        e["score"] = round(score, 2)

    if args.json:
        print(json.dumps({"header": header, "entries": entries}, indent=2, ensure_ascii=False))
        return 0

    print(f"# {header.get('list', '?')} - printed {header.get('printed', '?')} ({args.pdf})\n")
    print("| page | kind | task | line | best match | score |")
    print("|---|---|---|---|---|---|")
    for e in entries:
        cell = lambda s: (s or "").replace("|", "\\|")
        print(f"| {e['page']} | {e['kind']} | {cell(e['task'])[:40]} | {cell(e['text'])} "
              f"| {e['match'] or '**NO MATCH**'} | {e['score']} |")
    misses = sum(1 for e in entries if not e["match"])
    print(f"\n{len(entries)} lines, {misses} with no match.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
