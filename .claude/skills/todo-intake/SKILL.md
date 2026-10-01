---
name: todo-intake
description: Turn a printed Microsoft To Do list (a new PDF in docs/to-do/) into roadmap.yaml items, with every line accounted for. Use when the owner drops a new to-do list, says "new to-do list", "to-do drop", "import my to-do", or runs /todo-intake. Interim pipeline until feat-ca-todo-import ships.
---

# To-do intake

The owner keeps ideas in Microsoft To Do and prints the list to PDF. This skill turns a
printout into roadmap items. **`roadmap.yaml` is the source of truth.** A to-do PDF is a
frozen snapshot: it is committed once and never edited or re-synced afterwards.

`$ARGUMENTS` may be a PDF path. If it is empty, look for new drops (step 1).

## 0. Retirement check

Open `roadmap.yaml` and find `feat-ca-todo-import`. If its state is `done`, the console
imports To Do directly. Stop and tell the owner to use the console import instead.

## 1. Find the drop

- New drops live at `docs/to-do/YYYY-MM-DD.pdf` (the print date). If the owner gave a file
  somewhere else, move it there with `git mv` or `mv`.
- A drop is new if no item's `source` cites its path:
  `grep -c "docs/to-do/<date>.pdf" roadmap.yaml` returns 0.
- `docs/to-do.pdf` is the 2026-09-29 printout. It has already been imported; leave it where
  it is, because many sources cite it.

## 2. Extract and check coverage

```
python bin/todo-extract.py docs/to-do/<date>.pdf          # coverage table
python bin/todo-extract.py docs/to-do/<date>.pdf --text   # raw page text
```

Read both outputs. The table matches by shared words and URLs, so treat it as a lead,
not a verdict:
- A **NO MATCH** is often a line covered in different words. Grep `roadmap.yaml` for the
  line's key nouns before creating anything.
- A high score can match the wrong item. Check that the item really says what the line says.
- A wrapped line can attach to the neighbouring entry. Use `--text` to see where it belongs.

## 3. Triage every line

Each task and sub-step gets exactly one outcome:

| Outcome | Action |
|---|---|
| **covered** | An existing item already captures it. Append the new citation to its `source` only if the line adds something. |
| **extends** | Add to an existing item's notes or `acceptance_criteria`, and append the citation to its `source`. |
| **new item** | Add an item with `state: draft` (epics: `funnel`), `owner_role: bootstrap`, and the right `parent`, `group`, and `phase`. Include `source: ["docs/to-do/<date>.pdf p.N (<short quote>)"]`. |
| **owner decision** | Add a `kind: decision` item with `state: awaiting_human`, options, and a `recommended` option. |
| **dismissed** | A duplicate, or not actionable. Give a one-line reason in the report. |

Rules:
- Follow the field schema in the `roadmap.yaml` header comment. Ids are lowercase and
  hyphenated, and never change once written.
- Place new items next to their parent, in the right section of the file.
- Don't prioritize. Never set `ready`. WSJF values on new epics and features are initial
  estimates for PI planning; say so if you add them.
- A sub-step usually belongs to its task's item, as notes or acceptance criteria. Give it
  its own item only when it is separate work.

## 4. Ask before guessing

Collect every line you can't interpret (an unclear reference, a title the owner half
remembered, a "?") into **one** batched AskUserQuestion, each with your best guess as the
first option. Don't invent meanings. If something is still open after the questions, put
it in the item's notes as "owner to confirm".

## 5. Lint

`python bin/roadmap-lint.py` must print `OK` with 0 warnings. If `meta.updated` is behind
today's date, set it to today.

## 6. Report

In the final message, list every line of the drop with its outcome and item id, grouped
by page, so the owner can check nothing was dropped. List the decisions you added and any
questions still open.

## 7. Commit

On a branch, never directly on `main`, commit the PDF and `roadmap.yaml` together:
`Import owner to-do drop YYYY-MM-DD`. Don't edit the PDF after that.
