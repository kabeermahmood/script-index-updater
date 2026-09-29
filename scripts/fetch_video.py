"""Fetch one video's transcript as clean, readable prose.

Usage: python fetch_video.py <video_url> <out_dir> [--lang LANG] [--raw]

Accepts any single-video reference - a watch URL, a youtu.be short link, a
/shorts/ or /embed/ URL, or a bare 11-character video ID - downloads its
transcript (manual captions preferred, auto-generated fallback) and writes

  <out_dir>/<Video Title>.txt

reflowed into paragraphs rather than the one-line-per-caption-cue shape the
raw caption track has. The unreflowed text is cached under
%TEMP%/index-scripts/videos/, and transcripts already pulled by a channel run
are reused from that cache too, so a video is never downloaded twice.

Machine-readable markers are printed for the GUI: TITLE:, VIDEO_ID:, KIND:,
SAVED: and a final SUMMARY: line.

Requires: pip install yt-dlp
"""
import argparse
import glob
import json
import os
import random
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_channel import (RETRY_WAITS, Cooldown,  # noqa: E402
                           cache_root, is_rate_limit, pick_track, vtt_to_text)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
SENT_END = re.compile(r"[.!?][\"')\]]?$")
PARA_CHARS = 400      # start looking for a sentence break past this length
BLOCK_WORDS = 70      # paragraph size when the captions carry no punctuation


def log(msg):
    print(msg, flush=True)


def normalize_video_url(raw):
    """Any single-video reference -> (watch_url, video_id), or (None, None)."""
    s = (raw or "").strip().strip('"').strip("'")
    if ID_RE.match(s):
        return f"https://www.youtube.com/watch?v={s}", s
    if not re.match(r"^https?://", s, re.I):
        s = "https://" + s
    m = (re.search(r"[?&]v=([A-Za-z0-9_-]{11})", s)
         or re.search(r"youtu\.be/([A-Za-z0-9_-]{11})", s)
         or re.search(r"/(?:shorts|embed|live|v)/([A-Za-z0-9_-]{11})", s))
    if not m:
        return None, None
    return f"https://www.youtube.com/watch?v={m.group(1)}", m.group(1)


def is_punctuated(words):
    """True when the caption track carries real sentence punctuation.

    Human-written captions run about one sentence end per 15-20 words;
    auto-generated ones have none at all. One per 100 words is a safe floor.
    """
    enders = sum(1 for w in words if SENT_END.search(w))
    return enders >= max(2, len(words) / 100)


def reflow(text):
    """Caption lines -> flowing paragraphs.

    Punctuated tracks break at sentence ends once a paragraph is long enough;
    unpunctuated ones (auto-captions) fall back to fixed-size blocks so the
    result is still readable instead of one unbroken wall of words.
    """
    words = " ".join(ln.strip() for ln in text.splitlines() if ln.strip()).split()
    if not words:
        return ""
    paras, cur = [], []
    if is_punctuated(words):
        for w in words:
            cur.append(w)
            if SENT_END.search(w) and sum(len(x) + 1 for x in cur) >= PARA_CHARS:
                paras.append(" ".join(cur))
                cur = []
    else:
        for w in words:
            cur.append(w)
            if len(cur) >= BLOCK_WORDS:
                paras.append(" ".join(cur))
                cur = []
    if cur:
        paras.append(" ".join(cur))
    out = "\n\n".join(paras)
    return re.sub(r"\s+([,.!?;:])", r"\1", out)  # tidy space before punctuation


def safe_name(title, video_id):
    name = re.sub(r'[<>:"/\\|?*]', "", title or "").strip().rstrip(".")
    name = re.sub(r"\s+", " ", name)[:120].strip()
    return name or f"video-{video_id}"


def find_cached(video_id):
    """The raw transcript on disk, from a previous single fetch or a channel run."""
    direct = os.path.join(cache_root(), "videos", f"{video_id}.txt")
    if os.path.exists(direct):
        return direct
    hits = glob.glob(os.path.join(cache_root(), "channel-*", "transcripts",
                                  f"{video_id}.txt"))
    return hits[0] if hits else None


def title_from_cache(video_id):
    """Recover a cached video's title without touching the network."""
    side = os.path.join(cache_root(), "videos", f"{video_id}.title")
    if os.path.exists(side):
        try:
            with open(side, encoding="utf-8") as f:
                return f.read().strip()
        except OSError:
            pass
    for tj in glob.glob(os.path.join(cache_root(), "channel-*", "tabs.json")):
        try:
            with open(tj, encoding="utf-8") as f:
                for t in json.load(f).get("tabs", []):
                    if t.get("video_id") == video_id and t.get("title"):
                        return t["title"].strip()
        except (OSError, ValueError):
            continue
    return ""


def fetch_one(ydl, url, lang):
    """Returns (title, raw_text, kind, error). Manual captions beat auto ones."""
    info = ydl.extract_info(url, download=False)
    title = (info.get("title") or "").strip()
    manual = pick_track(info.get("subtitles") or {}, lang)
    tracks, kind = (manual, "manual") if manual else (
        pick_track(info.get("automatic_captions") or {}, lang), "auto")
    if not tracks:
        return title, None, "", f"no {lang} captions available"
    fmt = next((t for t in tracks if t.get("ext") == "vtt"), tracks[0])
    data = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
    text = vtt_to_text(data) if fmt.get("ext") == "vtt" else data
    if not text.strip():
        return title, None, kind, "caption track was empty"
    return title, text, kind, None


def resolve_one(ydl, ref, lang, vcache, raw_mode=False, no_cache=False,
                cool=None):
    """Fetch (or reuse from cache) one video's transcript.
    Returns a record dict, or one carrying "error"."""
    url, vid = normalize_video_url(ref.get("url") or ref.get("id") or "")
    if not url:
        return {"error": "not a YouTube video link",
                "title": ref.get("title", ""), "url": ref.get("url", "")}
    title = (ref.get("title") or "").strip()
    raw, kind, cached = None, "", False
    hit = None if no_cache else find_cached(vid)
    if hit:
        try:
            with open(hit, encoding="utf-8") as f:
                raw = f.read()
            kind, cached = "cached", True
            title = title or title_from_cache(vid)
        except OSError:
            raw = None
    if raw is None:
        # A 429 means "slow down", not "give up": without this the video was
        # reported as an anomaly and silently left out of the transcript.
        cool = cool or Cooldown()
        fetched_title, err = "", None
        for attempt, base_wait in enumerate(RETRY_WAITS):
            cool.wait()
            try:
                fetched_title, raw, kind, err = fetch_one(ydl, url, lang)
            except Exception as e:
                fetched_title, raw, kind, err = "", None, "", f"fetch failed: {e}"
            if raw or not is_rate_limit(err):
                break
            wait = base_wait + random.uniform(0, base_wait * 0.3)
            cool.trigger(wait)
            log(f"    rate limited by YouTube - waiting {wait:.0f}s "
                f"(attempt {attempt + 1}/{len(RETRY_WAITS)})")
            time.sleep(wait * 0.3)
        if not raw:
            return {"error": err, "title": title or fetched_title,
                    "id": vid, "url": url}
        title = title or fetched_title
        try:
            with open(os.path.join(vcache, f"{vid}.txt"), "w", encoding="utf-8") as f:
                f.write(raw)
        except OSError:
            pass
    if not title:  # cached transcript whose title we never recorded
        try:
            title = (ydl.extract_info(url, download=False).get("title") or "").strip()
        except Exception:
            title = ""
    if title:
        try:
            with open(os.path.join(vcache, f"{vid}.title"), "w", encoding="utf-8") as f:
                f.write(title)
        except OSError:
            pass
    text = raw if raw_mode else reflow(raw)
    if not text.strip():
        return {"error": "empty after cleaning", "title": title, "id": vid, "url": url}
    return {"title": title, "id": vid, "url": url, "text": text,
            "kind": kind, "cached": cached, "views": ref.get("views")}


def combine(results, label):
    """Several transcripts into one file, each under its own header."""
    head = label or "Transcripts"
    parts = [f"{head} - {len(results)} transcripts", ""]
    for n, r in enumerate(results, 1):
        views = f" - {r['views']:,} views" if isinstance(r.get("views"), int) else ""
        parts += ["=" * 78, f"{n}. {r['title']}", f"   {r['url']}{views}",
                  "=" * 78, "", r["text"], "", ""]
    return "\n".join(parts).rstrip() + "\n"


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("video_url", nargs="?",
                    help="a single video reference (omit when using --batch)")
    ap.add_argument("out_dir")
    ap.add_argument("--batch", help="JSON list of {id,title,url,views} to fetch "
                                    "into one combined file")
    ap.add_argument("--label", default="",
                    help="name the combined file after this (e.g. the channel)")
    ap.add_argument("--lang", default="en", help="caption language (default en)")
    ap.add_argument("--raw", action="store_true",
                    help="keep one line per caption cue instead of reflowing")
    ap.add_argument("--no-cache", action="store_true",
                    help="always re-download, ignoring any cached transcript")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between network fetches in batch mode")
    args = ap.parse_args()

    if args.batch:
        try:
            with open(args.batch, encoding="utf-8") as f:
                refs = json.load(f)
        except (OSError, ValueError) as e:
            sys.exit(f"ERROR: could not read the batch file: {e}")
        if not isinstance(refs, list) or not refs:
            sys.exit("ERROR: the batch file must hold a non-empty JSON list")
    elif args.video_url:
        refs = [{"url": args.video_url}]
    else:
        sys.exit("ERROR: give a video URL, or --batch <file>")

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        sys.exit("yt-dlp is not installed - run: pip install yt-dlp")

    vcache = os.path.join(cache_root(), "videos")
    os.makedirs(vcache, exist_ok=True)
    os.makedirs(args.out_dir, exist_ok=True)
    ydl = YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True})

    # one brake for the whole batch: a 429 on video 2 also holds back video 3
    cool = Cooldown()
    results, failures = [], []
    for n, ref in enumerate(refs, 1):
        if not isinstance(ref, dict):
            ref = {"url": str(ref)}
        shown = (ref.get("title") or ref.get("url") or ref.get("id") or "")[:70]
        log(f"[{n}/{len(refs)}] {shown}")
        rec = resolve_one(ydl, ref, args.lang, vcache, args.raw, args.no_cache,
                          cool)
        if rec.get("error"):
            failures.append(rec)
            log(f"    skipped - {rec['error']}")
            log(f"ANOMALY: \"{rec.get('title') or rec.get('url')}\": {rec['error']}")
            continue
        results.append(rec)
        log(f"    {'cached' if rec['cached'] else 'fetched'} - "
            f"{len(rec['text'].split()):,} words")
        if not rec["cached"] and n < len(refs):
            time.sleep(args.delay)  # stay polite between real network fetches

    if not results:
        sys.exit("ERROR: no transcript could be fetched")

    if len(results) == 1:
        rec = results[0]
        title, text = rec["title"], rec["text"]
        out_path = os.path.join(args.out_dir, safe_name(title, rec["id"]) + ".txt")
        log(f"VIDEO_ID: {rec['id']}")
        log(f"KIND: {rec['kind']}")
    else:
        title = f"{args.label or 'Transcripts'} - {len(results)} transcripts"
        text = combine(results, args.label)
        out_path = os.path.join(
            args.out_dir,
            f"{safe_name(args.label or 'Transcripts', 'batch')} "
            f"- {len(results)} transcripts.txt")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(text)

    words = text.split()
    # Whether the track is punctuated matters more than whether YouTube called
    # it manual or automatic: modern auto-captions often arrive fully
    # punctuated, so "kind" is a poor guide to how much polishing is needed.
    punct = is_punctuated(words)
    cached_n = sum(1 for r in results if r["cached"])
    log(f"TITLE: {title or '(untitled)'}")
    log(f"SAVED: {out_path}")
    log(f"SUMMARY: videos={len(results)} failed={len(failures)} "
        f"words={len(words)} chars={len(text)} "
        f"punctuated={'yes' if punct else 'no'} "
        f"cached={'yes' if cached_n == len(results) else 'no'}")


if __name__ == "__main__":
    main()
