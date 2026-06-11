---
name: index-scripts
description: Build or update a Word master list of YouTube video scripts (title + vehicle in context) from a Google Docs PDF export. Use this whenever the user provides a PDF of script tabs and wants them indexed, catalogued, or added to their existing list — including phrases like "index these scripts", "add the new scripts to the list", "update the master list", "make the title/vehicle list from this PDF", or when they drop a scripts PDF and ask for a list of titles and vehicles. Appends only NEW entries to the existing master; never rebuilds it from scratch.
---

# Index Scripts: PDF → Word master list

Turn a Google Docs PDF export of video-script tabs into rows of
**Tab # | Video Title | Vehicle in Context** in a Word master list,
appending only the scripts that are not already in the list.

## Inputs

1. **PDF path(s)** — required; one or more. Ask the user if not given. With
   multiple PDFs, run the full workflow for each in the given order, finishing
   the append before starting the next PDF so later ones dedupe against
   entries just added.
2. **Master docx path** — the list to append to. Default: the `master` value
   in `gui/config.json` next to this skill (the GUI keeps it updated with the
   last-used path); if that file is missing, ask the user. If the master docx
   does not exist, it will be created automatically from the bundled template.

## Workflow

Work in a temp directory (e.g. `%TEMP%\index-scripts\`) for intermediate files.

### Step 1 — Extract tabs from the PDF

```
python <skill>/scripts/extract_tabs.py "<pdf>" tabs.json dump.txt
```

Produces `tabs.json` with one record per tab: `{tab, title, vic, snippet}`.
`vic` is the script's explicit "Vehicle in Context:" label when present
(often empty). `dump.txt` is the full PDF text for deeper lookups.

**Handle the reported anomalies before moving on:**
- *Tab numbering gap* (e.g. tab 86 missing): the PDF export occasionally drops
  a tab's marker page, so that tab's script gets merged into the previous
  segment. Search `dump.txt` inside the segment before the gap for a second
  title-like line (a quoted, Title-Case headline). If found, treat it as its
  own entry with the missing tab number.
- *Oversized segment / malformed title*: read that part of `dump.txt` and fix
  the title manually. Some tabs are not scripts at all (research notes,
  brainstorms) — list them with a `(Not a script — …)` note in the title and
  the vehicle set to what fits (e.g. "Multiple vehicles").

### Step 2 — Compare against the master

```
python <skill>/scripts/compare.py tabs.json "<master.docx>" cmp.json
```

Buckets every extracted tab as **matched** (already listed), **uncertain**
(similar to an existing entry — review each one in `cmp.json` and decide:
same script reworded → skip; genuinely different → treat as new), or **new**.

Each uncertain/new record includes `master_entry_with_same_tab_number`. If
that master entry is clearly the same script under a cleaned-up or annotated
title (e.g. a "(Not a script — …)" note), it is NOT new — skip it. But don't
trust tab numbers alone: they shift when the user reorders or deletes tabs.

Duplicate titles *within* the PDF can also appear (the same script pasted in
two tabs). If two new tabs have near-identical titles, include both but mark
the later one `(duplicate of Tab N)` so the user can clean up their doc.

### Step 3 — Identify the vehicle for each NEW tab

For every entry in the `new` bucket (plus accepted uncertains):

- If `vic` is non-empty, use the vehicle name from it — just the designation,
  drop trailing descriptions after a dash (e.g. "FV4201 Chieftain — 1960s
  British MBT…" → "FV4201 Chieftain").
- Otherwise identify the featured vehicle from the title + snippet. It is the
  specific named vehicle/weapon the script is about, not ones mentioned in
  passing. Use the most specific common designation ("Sherman Crab",
  "Supacat Jackal (HMT 400)", "FV432 Mk3 Bulldog").
- If the snippet doesn't name it, read further into that script in `dump.txt`
  (the vehicle is usually named within the first two pages).
- If there are more than ~25 new tabs, split the work across 3–4 parallel
  general-purpose agents (give each a slice of the records and the dump path;
  have them return `tab|title|vehicle` lines).

Clean each title: strip stray `Title:` prefixes, outer quotes, leading digits,
and any leaked `Context:` fragments. Keep the title otherwise verbatim.

### Step 4 — Append to the master

Write the new entries (in tab-number order) to `rows.json` as a list of
`{"tab": …, "title": …, "vehicle": …}`, then:

```
python <skill>/scripts/append_master.py "<master.docx>" rows.json
```

This clones the last table row so fonts/borders/widths are preserved, and
creates the file from `assets/master_template.docx` when it doesn't exist.
Never regenerate the whole document — existing rows must stay untouched.

If the master is open in Word, saving fails with a permission error — ask the
user to close it and retry.

### Step 5 — Report

Tell the user: how many tabs were in the PDF, how many were already listed,
how many were appended (with a short table of the new title + vehicle pairs),
plus any anomalies (gaps, duplicates, non-script tabs) they may want to fix
in their Google Doc. Clean up the temp files.

## Dependencies

`pip install pymupdf python-docx`
