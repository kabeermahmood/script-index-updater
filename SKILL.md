---
name: index-scripts
description: Build or update a Word master list of YouTube video scripts (title + vehicle in context) from a Google Docs PDF export or directly from a YouTube channel URL. Use this whenever the user provides a PDF of script tabs OR a channel URL and wants the videos indexed, catalogued, or added to their existing list — including phrases like "index these scripts", "add the new scripts to the list", "update the master list", "make the title/vehicle list from this PDF", "index my channel", "download all the transcripts from this channel and index them". Appends only NEW entries to the existing master; never rebuilds it from scratch.
---

# Index Scripts: PDF or YouTube channel → Word master list

Turn a Google Docs PDF export of video-script tabs — or every video
transcript on a YouTube channel — into rows of
**Tab # | Video Title | Vehicle in Context** in a Word master list,
appending only the scripts that are not already in the list.

## Inputs

1. **PDF path(s) and/or a YouTube channel URL** — at least one source is
   required. Ask the user if not given. With multiple PDFs, run the full
   workflow for each in the given order, finishing the append before starting
   the next PDF so later ones dedupe against entries just added. When a
   channel URL is given alongside PDFs, process the PDFs first, then the
   channel.
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

### Step 1 (channel mode) — Fetch transcripts from a YouTube channel

When the source is a channel URL instead of a PDF (skip this step if the
prompt says the transcripts are already fetched — the GUI pre-fetches them
and gives you the tabs.json path):

```
python <skill>/scripts/fetch_channel.py "<channel_url>" <workdir>/channel
```

This enumerates every video on the channel's Videos tab and downloads each
transcript (manual captions preferred, auto-generated fallback), producing
`<workdir>/channel/tabs.json` in the **same schema as extract_tabs.py** plus
plain-text transcripts in `<workdir>/channel/transcripts/`. Continue with
Step 2 exactly as for a PDF. Channel-mode notes:

- `tab` numbers are upload order (1 = oldest video), so they stay stable as
  new videos are published. Titles are the video titles verbatim.
- Each record carries a `transcript_file` path — in Step 3, read that file
  (instead of `dump.txt`) when the snippet doesn't reveal the vehicle.
- Transcripts are cached on disk; re-running after new uploads only fetches
  the new videos. A large channel takes ~2s per uncached video — for a first
  run over hundreds of videos, warn the user it will take a few minutes.
- Auto-captions lack punctuation and may mis-hear designations
  ("Bismarck" → "bismark") — fine for identifying the vehicle, but never
  copy designations from the transcript without sanity-checking the spelling.
- Videos with no captions at all are listed in `anomalies` — report them at
  the end; there is nothing to fix.

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

**Optional — angle/summary:** when the user asked for it (the GUI's
"Angle / summary column" toggle, or a request like "include summaries"),
also write an `angle` for each new entry: 1–2 sentences (max ~30 words)
capturing the script's specific hook — the story it tells about the vehicle
(e.g. "A written-off armoured car becomes Burma's most feared convoy
escort"), never a generic description of the vehicle.

### Step 4 — Append to the master

Write the new entries (in tab-number order) to `rows.json` as a list of
`{"tab": …, "title": …, "vehicle": …}` — plus `"angle": …` when summaries
were requested (the script then auto-upgrades 3-column masters to 4 columns;
existing rows keep an empty angle cell) — then:

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

`pip install pymupdf python-docx yt-dlp`
