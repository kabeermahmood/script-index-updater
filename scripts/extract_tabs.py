"""Extract script tabs from a Google Docs PDF export.

Usage: python extract_tabs.py <pdf_path> <out_json> [<out_dump_txt>]

The PDF is expected to be a Google Doc export where each tab starts with a
page containing only "Tab N", followed by the script (title first, then an
optional "Vehicle in Context:" line, then the body).

Writes a JSON list of {tab, title, vic, snippet} plus an "anomalies" report
(numbering gaps, suspiciously long titles, suspiciously long segments that
may contain two merged scripts). Optionally writes the full text dump so the
caller can read deeper into any script.
"""
import sys, re, json
import fitz

PAGE_BREAK = "\n<<<PAGE>>>\n"


def clean_title(raw):
    t = raw.strip()
    # strip stray leading digits/markers like "1Why" or "Title:"
    t = re.sub(r"^\s*Title\s*:\s*", "", t, flags=re.I)
    t = re.sub(r"^([0-9]\s*)?(?=[A-Z\"'“‘])", "", t)
    # cut off embedded context labels that belong to the next block
    parts = re.split(
        r"\s*(?:Vehicle\s+in\s+Context|Weapon\s+in\s+Context|Context\s+Vehicle|Context)\s*:",
        t, maxsplit=1, flags=re.I)
    t = parts[0]
    rest = parts[1].strip() if len(parts) > 1 else ""
    t = t.strip().strip('"“”').strip()
    # remove zero-width and stray control chars
    t = re.sub(r"[​‌‍﻿ ]", "", t).strip()
    return t, rest


def parse_segment(seg):
    seg = seg.replace("<<<PAGE>>>", "\n\n")
    blocks, cur = [], []
    for line in seg.split("\n"):
        if line.strip():
            cur.append(line.strip())
        elif cur:
            blocks.append(" ".join(cur))
            cur = []
    if cur:
        blocks.append(" ".join(cur))
    if not blocks:
        return "", "", ""
    title, title_rest = clean_title(blocks[0])
    vic = ""
    body_start = 1
    for i, b in enumerate(blocks[1:5], start=1):
        m = re.match(r"(?:vehicle|weapon)\s+in\s+context\s*:?\s*(.*)", b, re.I)
        if m:
            vic = m.group(1).strip()
            body_start = i + 1
            break
    if not vic and title_rest:
        vic = title_rest
    snippet = " ".join(blocks[body_start:])[:2500]
    return title, vic, snippet


def main():
    pdf_path, out_json = sys.argv[1], sys.argv[2]
    dump_path = sys.argv[3] if len(sys.argv) > 3 else None

    doc = fitz.open(pdf_path)
    text = PAGE_BREAK.join(p.get_text() for p in doc)
    if dump_path:
        with open(dump_path, "w", encoding="utf-8") as f:
            f.write(text)

    parts = re.split(r"(?:^|\n)\s*Tab (\d+)\s*\n", text)
    nums = [int(n) for n in parts[1::2]]
    segments = parts[2::2]

    anomalies = []
    if not nums:
        anomalies.append("No 'Tab N' markers found - is this a Google Docs tab export?")
    else:
        expected = set(range(min(nums), max(nums) + 1))
        gaps = sorted(expected - set(nums))
        if gaps:
            anomalies.append(
                f"Tab numbering gaps: {gaps}. The PDF export sometimes drops a tab's "
                f"marker page, merging its script into the previous tab's segment. "
                f"Check the segment before each gap for a second embedded title.")

    lens = sorted(len(s) for s in segments) if segments else [0]
    median = lens[len(lens) // 2]

    tabs = []
    for n, seg in zip(nums, segments):
        title, vic, snippet = parse_segment(seg)
        if len(title) > 200:
            anomalies.append(f"Tab {n}: title looks malformed/too long - review manually.")
        if median and len(seg) > 1.8 * median:
            anomalies.append(
                f"Tab {n}: segment is {len(seg)} chars vs median {median} - "
                f"may contain two merged scripts.")
        tabs.append({"tab": n, "title": title, "vic": vic, "snippet": snippet})

    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"tabs": tabs, "anomalies": anomalies}, f, indent=1, ensure_ascii=False)
    print(f"tabs: {len(tabs)} | anomalies: {len(anomalies)}")
    for a in anomalies:
        print("ANOMALY:", a)


if __name__ == "__main__":
    main()
