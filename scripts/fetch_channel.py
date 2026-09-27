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
import concurrent.futures
import json
import os
import random
import re
import sys
import threading
import time

SNIPPET_CHARS = 1200
TAG_RE = re.compile(r"<[^>]+>")
TS_LINE = re.compile(r"^\d{2}:\d{2}:\d{2}[.,]\d{3}\s+-->")
RETRY_WAITS = (10, 30, 90)  # seconds, + jitter


def log(msg):
    print(msg, flush=True)


def cache_root():
    """Where transcripts are cached between runs.

    Deliberately NOT the system temp dir: Windows Storage Sense and Disk
    Cleanup purge %TEMP%, which silently threw away every cached transcript
    and made each re-run download the whole channel again.
    """
    override = os.environ.get("INDEX_SCRIPTS_CACHE")
    if override:
        return override
    base = os.environ.get("LOCALAPPDATA") if os.name == "nt" else None
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "ScriptIndexUpdater", "cache")


def channel_cache_key(url):
    """Stable cache identity for a channel. The same channel typed any of its
    usual ways ("@Chan", "@Chan/", ".../videos", with or without scheme or
    www.) must land in ONE cache directory - hashing the raw string orphaned
    the cache and silently re-downloaded the whole channel."""
    u = normalize_channel_url(url).lower()
    u = re.sub(r"^https?://", "", u)
    return re.sub(r"^www\.", "", u)


def channel_master_path(master_dir, channel_name):
    """`<dir>/<Channel> Script Index.docx` - the per-channel master list.
    Shared with the GUI so both agree on the path before anything is fetched."""
    safe = re.sub(r'[<>:"/\\|?*]', "", channel_name or "").strip()
    if not safe:
        return ""
    return os.path.join(master_dir or ".", f"{safe} Script Index.docx")


class Cooldown:
    """Shared rate-limit brake: any worker can pause the whole pool."""

    def __init__(self):
        self._lock = threading.Lock()
        self._until = 0.0

    def wait(self):
        while True:
            with self._lock:
                remaining = self._until - time.time()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 1.0))

    def trigger(self, secs):
        """Extend the cooldown. Returns True if this call extended it."""
        with self._lock:
            target = time.time() + secs
            if target > self._until:
                self._until = target
                return True
            return False


def is_rate_limit(err):
    msg = str(err).lower()
    return "429" in msg or "too many requests" in msg or "rate limit" in msg


def load_view_cache(out_dir):
    """Previously collected view counts for this channel, {video_id: int}."""
    try:
        with open(os.path.join(out_dir, "views.json"), encoding="utf-8") as f:
            data = json.load(f)
        return {k: v for k, v in (data.get("views") or {}).items()
                if isinstance(v, int)}
    except (OSError, ValueError):
        return {}


def save_view_cache(out_dir, views):
    try:
        os.makedirs(out_dir, exist_ok=True)
        with open(os.path.join(out_dir, "views.json"), "w", encoding="utf-8") as f:
            json.dump({"views": views, "saved": time.time()}, f)
    except OSError:
        pass


def collect_views(ydl_factory, videos, cached, out_dir, workers, delay):
    """View count for every video that has none yet.

    YouTube no longer returns view_count in the channel listing (4 of 81 on a
    real channel), so each one costs a full extraction. They are cached, so a
    channel pays this once and later runs only price new uploads.
    """
    missing = [v for v in videos
               if v.get("views") is None and v["id"] not in cached]
    if not missing:
        log(f"View counts: all {len(videos)} already known")
        return cached
    log(f"Fetching view counts for {len(missing)} video(s) "
        f"({len(cached)} already cached)")
    cool = Cooldown()
    local = threading.local()

    def get_ydl():
        y = getattr(local, "ydl", None)
        if y is None:
            y = ydl_factory({"quiet": True, "no_warnings": True,
                             "skip_download": True})
            local.ydl = y
        return y

    def one(v):
        for attempt, base_wait in enumerate(RETRY_WAITS):
            cool.wait()
            try:
                info = get_ydl().extract_info(v["url"], download=False)
                time.sleep(delay)
                return v["id"], info.get("view_count")
            except Exception as e:
                if not is_rate_limit(e):
                    return v["id"], None
                wait = base_wait + random.uniform(0, base_wait * 0.3)
                if cool.trigger(wait):
                    log(f"    rate limited by YouTube - pausing every worker "
                        f"for {wait:.0f}s (attempt {attempt + 1}/"
                        f"{len(RETRY_WAITS)})")
                time.sleep(wait * 0.3)
        return v["id"], None

    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        for fut in concurrent.futures.as_completed(
                [ex.submit(one, v) for v in missing]):
            vid, count = fut.result()
            done += 1
            if count is not None:
                cached[vid] = count
            log(f"[{done}/{len(missing)}] views "
                f"{format(count, ',') if count is not None else 'unavailable'}")
    save_view_cache(out_dir, cached)
    return cached


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
    ap.add_argument("--master", default="",
                    help="master .docx: videos already listed in it are not "
                         "fetched at all")
    ap.add_argument("--master-dir", default="",
                    help="folder holding '<Channel> Script Index.docx'; the "
                         "master is resolved once the channel name is known")
    ap.add_argument("--workers", type=int, default=3,
                    help="parallel fetch workers (default 3)")
    ap.add_argument("--with-views", action="store_true",
                    help="with --list-only, also collect each video's view "
                         "count. YouTube stopped putting these in the channel "
                         "listing, so they cost one request per video; results "
                         "are cached and only missing ones are fetched.")
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
        vcache = os.path.join(cache_root(), "videos")
        views = load_view_cache(args.out_dir)
        if args.with_views:
            views = collect_views(YoutubeDL, videos, views, args.out_dir,
                                  max(1, args.workers), args.delay)
        listing = [{"tab": n, "id": v["id"], "title": v["title"], "url": v["url"],
                    # the listing itself rarely carries view_count any more, so
                    # fall back to whatever --with-views has cached
                    "views": v.get("views") if v.get("views") is not None
                             else views.get(v["id"]),
                    "duration": v.get("duration"),
                    "cached": (os.path.exists(os.path.join(tdir, f"{v['id']}.txt"))
                               or os.path.exists(os.path.join(vcache, f"{v['id']}.txt")))}
                   for n, v in enumerate(videos, 1)]
        known_views = sum(1 for e in listing if e["views"] is not None)
        log(f"VIEWS: {known_views}/{len(listing)}")
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

    # --- skip anything the master already lists -----------------------------
    # The match is decided by TITLE alone (compare.py does the same), and the
    # titles came free with the listing above - so there is no reason to
    # download a transcript for a video that is already indexed.
    master = args.master
    if not master and args.master_dir:
        master = channel_master_path(args.master_dir, channel)
    known_norms = []
    if master:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        try:
            from compare import already_indexed, norm, read_master_titles
            existing = read_master_titles(master)
            known_norms = [(norm(e["title"]), e) for e in existing]
            log(f"Master list holds {len(existing)} entry(s): {master}")
        except Exception as e:
            log(f"Could not read the master list ({e}) - fetching every video")
            known_norms = []

    tabs, anomalies = [], []
    todo, known = [], 0
    for n, v in numbered:
        if known_norms and already_indexed(v["title"], known_norms):
            known += 1
            # Still listed so compare.py counts it as "matched" and the run
            # report stays honest - it just carries no transcript.
            tabs.append({"tab": n, "title": v["title"], "vic": "", "snippet": "",
                         "video_id": v["id"], "url": v["url"],
                         "transcript_file": "", "already_indexed": True})
        else:
            todo.append((n, v))
    if known:
        log(f"{known} video(s) already in the master list - skipping their transcripts")
    log(f"{len(todo)} video(s) need a transcript")

    fetched = cached = skipped = 0
    cool = Cooldown()
    local = threading.local()

    def get_ydl():
        y = getattr(local, "ydl", None)
        if y is None:  # one extractor per worker: not shared across threads
            y = YoutubeDL({"quiet": True, "no_warnings": True,
                           "skip_download": True})
            local.ydl = y
        return y

    def fetch_one(item):
        """Returns (n, video, text, err, source)."""
        n, v = item
        txt_path = os.path.join(tdir, f"{v['id']}.txt")
        none_path = os.path.join(tdir, f"{v['id']}.none")
        if os.path.exists(txt_path):
            try:
                with open(txt_path, encoding="utf-8") as f:
                    return n, v, f.read(), None, "cached"
            except OSError:
                pass
        if os.path.exists(none_path):
            return n, v, None, "no captions available", "failed"
        text = err = None
        for attempt, base_wait in enumerate(RETRY_WAITS):
            cool.wait()
            try:
                text, err = fetch_transcript(get_ydl(), v, args.lang)
            except Exception as e:
                text, err = None, f"fetch failed: {e}"
            if text:
                try:
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(text)
                except OSError:
                    pass
                time.sleep(args.delay)  # stay polite after a real request
                return n, v, text, None, "fetched"
            if not is_rate_limit(err):
                break
            wait = base_wait + random.uniform(0, base_wait * 0.3)
            if cool.trigger(wait):
                log(f"    rate limited by YouTube - pausing every worker for "
                    f"{wait:.0f}s (attempt {attempt + 1}/{len(RETRY_WAITS)})")
            time.sleep(wait * 0.3)
        # a 429 gets no .none marker, so the next run retries it
        if "no captions" in (err or ""):
            try:
                with open(none_path, "w", encoding="utf-8") as f:
                    f.write(err)
            except OSError:
                pass
        time.sleep(args.delay)
        return n, v, None, err, "failed"

    got, done_n, total = [], 0, len(todo)
    if todo:
        with concurrent.futures.ThreadPoolExecutor(
                max_workers=max(1, args.workers)) as ex:
            for fut in concurrent.futures.as_completed(
                    [ex.submit(fetch_one, it) for it in todo]):
                n, v, text, err, source = fut.result()
                done_n += 1
                label = f"[{done_n}/{total}] {v['title'][:70]}"
                if source == "cached":
                    cached += 1
                    log(f"{label} - cached")
                elif source == "fetched":
                    fetched += 1
                    log(f"{label} - transcript saved ({len(text):,} chars)")
                else:
                    skipped += 1
                    anomalies.append((n, f"Video {n} \"{v['title']}\" "
                                         f"({v['id']}): {err} - skipped"))
                    log(f"{label} - {err}, skipped")
                if text:
                    got.append((n, v, text))

    for n, v, text in got:
        snippet = re.sub(r"\s+", " ", text[:SNIPPET_CHARS * 2]).strip()[:SNIPPET_CHARS]
        tabs.append({"tab": n, "title": v["title"], "vic": "",
                     "snippet": snippet, "video_id": v["id"], "url": v["url"],
                     "transcript_file": os.path.abspath(
                         os.path.join(tdir, f"{v['id']}.txt"))})
    # workers finish out of order; restore upload order for everything below
    tabs.sort(key=lambda t: t["tab"])
    anomalies = [msg for _, msg in sorted(anomalies)]

    out = {"channel": channel, "url": url, "tabs": tabs, "anomalies": anomalies}
    tabs_json = os.path.join(args.out_dir, "tabs.json")
    with open(tabs_json, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)
    log(f"\n{len(tabs)} tab(s) ready ({fetched} fetched, {cached} cached, "
        f"{known} already indexed, {skipped} without captions) -> {tabs_json}")


if __name__ == "__main__":
    main()
