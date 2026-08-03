"""Fetch every video transcript from a YouTube channel.

Usage: python fetch_channel.py <channel_url> <out_dir> [--max N] [--delay SECS] [--lang LANG]

Enumerates all videos on the channel's Videos tab (numbered oldest-first, so
numbers stay stable as new videos are published), downloads each video's
transcript (manual captions preferred, auto-generated fallback), and writes:

  <out_dir>/transcripts/<video_id>.txt   plain-text transcript per video
  <out_dir>/tabs.json                    same schema as extract_tabs.py:
      {"channel", "url", "tabs": [{tab, title, vic, snippet, video_id,
       url, transcript_file}], "anomalies": [...]}

so the rest of the pipeline (compare.py -> identify -> append_master.py)
works on channels exactly as it does on PDF exports.

Transcripts already on disk are skipped, so re-running after new uploads only
fetches what's new. Videos with no captions at all get a <video_id>.none
marker so they are not re-requested every run. A polite delay (default 1.5s)
separates network fetches to stay under YouTube's rate limits.

Requires: pip install yt-dlp
"""
import argparse
import json
import os
import re
import sys
import tempfile
import time

SNIPPET_CHARS = 1200
TAG_RE = re.compile(r"<[^>]+>")
TS_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->")


def log(msg):
    print(msg, flush=True)


def normalize_channel_url(url):
    url = url.strip().strip('"').strip("'")
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    # channel root (handle /@name, /channel/UC…, /c/name, /user/name) -> Videos tab
    if re.search(r"youtube\.com", url, re.I) and not re.search(
            r"/(videos|streams|shorts|playlist|watch)\b", url, re.I):
        url = url.rstrip("/") + "/videos"
    return url


def vtt_to_text(vtt):
    """Strip WEBVTT headers, timestamps and tags; collapse the rolling
    duplicate lines that auto-generated captions produce."""
    out, last = [], ""
    for ln in vtt.splitlines():
        ln = ln.strip()
        if (not ln or ln == "WEBVTT" or TS_LINE.match(ln) or ln.isdigit()
                or ln.startswith(("Kind:", "Language:", "NOTE", "STYLE", "Style:"))):
            continue
        ln = TAG_RE.sub("", ln)
        ln = (ln.replace("&nbsp;", " ").replace("&amp;", "&")
                .replace("&gt;", ">").replace("&lt;", "<").replace("&#39;", "'").strip())
        if not ln or ln == last:
            continue
        out.append(ln)
        last = ln
    return "\n".join(out)


def list_videos(ydl, url):
    """Flat-extract the channel's Videos tab. Returns (channel_name, videos
    oldest-first as [{id, title, url}])."""
    info = ydl.extract_info(url, download=False)
    entries = list(info.get("entries") or [])
    videos = []
    for e in entries:
        if not e or not e.get("id"):
            continue
        videos.append({
            "id": e["id"],
            "title": (e.get("title") or "").strip(),
            "url": e.get("url") or f"https://www.youtube.com/watch?v={e['id']}",
            # the flat extract carries these already - no extra request per video
            "views": e.get("view_count"),
            "duration": e.get("duration"),
        })
    videos.reverse()  # YouTube lists newest first; we number oldest-first
    name = info.get("channel") or info.get("uploader") or info.get("title") or ""
    return name, videos


def pick_track(tracks_by_lang, lang):
    for key in (lang, f"{lang}-US", f"{lang}-GB", f"{lang}-orig"):
        if key in tracks_by_lang:
            return tracks_by_lang[key]
    for key in tracks_by_lang:
        if key.lower().startswith(lang.lower()):
            return tracks_by_lang[key]
    return None


def fetch_transcript(ydl, video, lang):
    """Returns (text, error). Manual subtitles win over auto captions."""
    info = ydl.extract_info(video["url"], download=False)
    tracks = (pick_track(info.get("subtitles") or {}, lang)
              or pick_track(info.get("automatic_captions") or {}, lang))
    if not tracks:
        return None, "no captions available"
    fmt = next((t for t in tracks if t.get("ext") == "vtt"), tracks[0])
    data = ydl.urlopen(fmt["url"]).read().decode("utf-8", "replace")
    text = vtt_to_text(data) if fmt.get("ext") == "vtt" else data
    if not text.strip():
        return None, "caption track was empty"
    return text, None


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channel_url")
    ap.add_argument("out_dir")
    ap.add_argument("--max", type=int, default=0,
                    help="only the N most recent videos (0 = all)")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="seconds between network fetches")
    ap.add_argument("--lang", default="en", help="caption language (default en)")
    ap.add_argument("--list-only", action="store_true",
                    help="just enumerate the channel's videos into videos.json; "
                         "download no transcripts")
    args = ap.parse_args()

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        sys.exit("yt-dlp is not installed - run: pip install yt-dlp")

    url = normalize_channel_url(args.channel_url)
    tdir = os.path.join(args.out_dir, "transcripts")
    os.makedirs(tdir, exist_ok=True)

    ydl = YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                     "extract_flat": "in_playlist"})
    log(f"Listing videos on {url} ...")
    try:
        channel, videos = list_videos(ydl, url)
    except Exception as e:
        sys.exit(f"ERROR: could not list channel videos: {e}")
    if not videos:
        sys.exit("ERROR: no videos found - is this a channel URL?")
    log(f"Channel: {channel or '(unknown)'} - {len(videos)} video(s) found")

    if args.list_only:
        # Enumeration only - what the GUI's single-video picker runs. "cached"
        # covers both a previous channel run and a previous single fetch.
        vcache = os.path.join(tempfile.gettempdir(), "index-scripts", "videos")
        listing = [{"tab": n, "id": v["id"], "title": v["title"], "url": v["url"],
                    "views": v.get("views"), "duration": v.get("duration"),
                    "cached": (os.path.exists(os.path.join(tdir, f"{v['id']}.txt"))
                               or os.path.exists(os.path.join(vcache, f"{v['id']}.txt")))}
                   for n, v in enumerate(videos, 1)]
        videos_json = os.path.join(args.out_dir, "videos.json")
        with open(videos_json, "w", encoding="utf-8") as f:
            json.dump({"channel": channel, "url": url, "videos": listing}, f,
                      indent=1, ensure_ascii=False)
        log(f"LIST: {videos_json}")
        log(f"{len(listing)} video(s) listed -> {videos_json}")
        return

    numbered = list(enumerate(videos, 1))  # (tab number, video), oldest first
    if args.max > 0:
        numbered = numbered[-args.max:]
        log(f"--max {args.max}: processing the {len(numbered)} most recent video(s)")

    tabs, anomalies = [], []
    fetched = cached = skipped = 0
    for n, v in numbered:
        txt_path = os.path.join(tdir, f"{v['id']}.txt")
        none_path = os.path.join(tdir, f"{v['id']}.none")
        label = f"[{n}/{len(videos)}] {v['title'][:70]}"
        text = None
        if os.path.exists(txt_path):
            with open(txt_path, encoding="utf-8") as f:
                text = f.read()
            cached += 1
            log(f"{label} - cached")
        elif os.path.exists(none_path):
            skipped += 1
            anomalies.append(f"Video {n} \"{v['title']}\" ({v['id']}): "
                             "no captions available - skipped")
            log(f"{label} - no captions (cached marker), skipped")
        else:
            try:
                text, err = fetch_transcript(ydl, v, args.lang)
            except Exception as e:
                text, err = None, f"fetch failed: {e}"
            if text:
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(text)
                fetched += 1
                log(f"{label} - transcript saved ({len(text):,} chars)")
            else:
                skipped += 1
                if "no captions" in (err or ""):
                    with open(none_path, "w", encoding="utf-8") as f:
                        f.write(err)
                anomalies.append(f"Video {n} \"{v['title']}\" ({v['id']}): {err} - skipped")
                log(f"{label} - {err}, skipped")
            time.sleep(args.delay)
        if text:
            snippet = re.sub(r"\s+", " ", text[:SNIPPET_CHARS * 2]).strip()[:SNIPPET_CHARS]
            tabs.append({"tab": n, "title": v["title"], "vic": "",
                         "snippet": snippet, "video_id": v["id"], "url": v["url"],
                         "transcript_file": os.path.abspath(txt_path)})

    out = {"channel": channel, "url": url, "tabs": tabs, "anomalies": anomalies}
    tabs_json = os.path.join(args.out_dir, "tabs.json")
    with open(tabs_json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    log(f"\n{len(tabs)} transcript(s) ready ({fetched} fetched, {cached} cached, "
        f"{skipped} without captions) -> {tabs_json}")


if __name__ == "__main__":
    main()
