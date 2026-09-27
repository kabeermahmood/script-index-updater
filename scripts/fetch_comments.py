"""Fetch every comment from every video on a YouTube channel.

Usage: python fetch_comments.py <channel_url> <out_dir>
           [--csv-dir DIR] [--max N] [--delay SECS] [--max-comments N]
           [--workers N]

Enumerates all videos on the channel's Videos tab (numbered oldest-first,
same as fetch_channel.py), downloads each video's full comment threads
(top-level comments and replies) via yt-dlp - no API key needed - and writes:

  <out_dir>/comments/<video_id>.json      raw per-video comment dump
  <csv-dir>/<Channel Name> Comments.json  the whole channel's comments, one
                                          structured file (primary export -
                                          ideal for AI analysis)
  <csv-dir>/<Channel Name> Comments.csv   the same data as a spreadsheet

Videos are fetched by a small pool of parallel workers (--workers, default 3)
with rate-limit awareness: a 429 from YouTube puts ALL workers on a shared
exponential-backoff cooldown, and each video is retried up to 3 times before
being skipped. Per-video dumps are cached on disk, so re-running only fetches
comments for new videos (the CSV is rebuilt from the cache every run, so it
always holds the full channel). With --max-comments the cap fetches the TOP
comments (most relevant first), not the newest.

The CSV is UTF-8 with BOM so Excel opens it correctly. Columns:
video #, video id, video title, comment id, parent id, reply?, author,
uploader?, likes, published (UTC), comment text.

Requires: pip install yt-dlp
"""
import argparse
import concurrent.futures
import csv
import datetime
import json
import os
import random
import re
import sys
import threading
import time

from fetch_channel import (RETRY_WAITS, Cooldown, is_rate_limit, list_videos,
                           log, normalize_channel_url)

KEEP = ("id", "parent", "text", "author", "author_is_uploader",
        "like_count", "timestamp")


def fetch_comments(video_url, max_comments):
    from yt_dlp import YoutubeDL
    eargs = {}
    if max_comments > 0:
        # cap = the TOP N comments, not the N newest
        eargs = {"youtube": {"max_comments": [str(max_comments)],
                             "comment_sort": ["top"]}}
    opts = {"quiet": True, "no_warnings": True, "skip_download": True,
            "getcomments": True, "extractor_args": eargs}
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(video_url, download=False)
    comments = [{k: c.get(k) for k in KEEP} for c in (info.get("comments") or [])]
    return comments


def published(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime(
        "%Y-%m-%d %H:%M")


def write_json(path, channel, url, dumps):
    out = {"channel": channel, "url": url,
           "videos": [{"n": d["n"], "video_id": d["video_id"],
                       "title": d["title"], "url": d.get("url", ""),
                       "comment_count": len(d["comments"]),
                       "comments": d["comments"]} for d in dumps]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=1, ensure_ascii=False)


def write_csv(path, dumps):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["Video #", "Video ID", "Video Title", "Comment ID",
                    "Parent ID", "Reply?", "Author", "Uploader?", "Likes",
                    "Published (UTC)", "Comment"])
        for d in dumps:
            for c in d["comments"]:
                parent = c.get("parent") or "root"
                w.writerow([d["n"], d["video_id"], d["title"], c.get("id", ""),
                            "" if parent == "root" else parent,
                            "no" if parent == "root" else "yes",
                            c.get("author", ""),
                            "yes" if c.get("author_is_uploader") else "no",
                            c.get("like_count") or 0,
                            published(c.get("timestamp")),
                            (c.get("text") or "").strip()])


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("channel_url")
    ap.add_argument("out_dir")
    ap.add_argument("--csv-dir", default="", help="folder for the CSV export "
                    "(default: out_dir)")
    ap.add_argument("--max", type=int, default=0,
                    help="only the N most recent videos (0 = all)")
    ap.add_argument("--delay", type=float, default=1.0,
                    help="seconds between fetches")
    ap.add_argument("--max-comments", type=int, default=0,
                    help="cap at the top N comments per video (0 = all)")
    ap.add_argument("--workers", type=int, default=3,
                    help="parallel fetch workers (default 3)")
    args = ap.parse_args()

    try:
        from yt_dlp import YoutubeDL
    except ImportError:
        sys.exit("yt-dlp is not installed - run: pip install yt-dlp")

    url = normalize_channel_url(args.channel_url)
    cdir = os.path.join(args.out_dir, "comments")
    os.makedirs(cdir, exist_ok=True)

    lister = YoutubeDL({"quiet": True, "no_warnings": True, "skip_download": True,
                        "extract_flat": "in_playlist"})
    log(f"Listing videos on {url} ...")
    try:
        channel, videos = list_videos(lister, url)
    except Exception as e:
        sys.exit(f"ERROR: could not list channel videos: {e}")
    if not videos:
        sys.exit("ERROR: no videos found - is this a channel URL?")
    log(f"Channel: {channel or '(unknown)'} - {len(videos)} video(s) found")

    numbered = list(enumerate(videos, 1))
    if args.max > 0:
        numbered = numbered[-args.max:]
        log(f"--max {args.max}: processing the {len(numbered)} most recent video(s)")

    cooldown = Cooldown()
    progress_lock = threading.Lock()
    completed = [0]
    total = len(numbered)

    def bump(title):
        with progress_lock:
            completed[0] += 1
            return f"[{completed[0]}/{total}] {title[:70]}"

    def process(n, v):
        """Fetch one video's comments (with retries). Returns (status, dump)."""
        jpath = os.path.join(cdir, f"{v['id']}.json")
        if os.path.exists(jpath):
            with open(jpath, encoding="utf-8") as f:
                dump = json.load(f)
            dump["n"] = n  # keep numbering current even for cached dumps
            log(f"{bump(v['title'])} - cached ({len(dump['comments'])} comments)")
            return "cached", dump
        last_err = None
        for attempt, base_wait in enumerate(RETRY_WAITS):
            cooldown.wait()
            try:
                comments = fetch_comments(v["url"], args.max_comments)
                break
            except Exception as e:
                last_err = e
                wait = base_wait + random.uniform(0, 5)
                if is_rate_limit(e):
                    if cooldown.trigger(wait):
                        log(f"Rate limited by YouTube - backing off {int(wait)}s "
                            f"(attempt {attempt + 1}/{len(RETRY_WAITS)})")
                else:
                    time.sleep(wait * 0.3)
        else:
            log(f"{bump(v['title'])} - fetch failed after "
                f"{len(RETRY_WAITS)} attempts: {last_err} - skipped")
            return "failed", None
        dump = {"n": n, "video_id": v["id"], "title": v["title"],
                "url": v["url"], "comments": comments}
        with open(jpath, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False)
        log(f"{bump(v['title'])} - {len(comments)} comment(s) saved")
        time.sleep(args.delay)
        return "fetched", dump

    dumps, fetched, cached, failed = [], 0, 0, 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(args.workers, 1)) as ex:
        futures = [ex.submit(process, n, v) for n, v in numbered]
        for fut in concurrent.futures.as_completed(futures):
            status, dump = fut.result()
            if status == "fetched":
                fetched += 1
            elif status == "cached":
                cached += 1
            else:
                failed += 1
            if dump:
                dumps.append(dump)
    dumps.sort(key=lambda d: d["n"])

    total = sum(len(d["comments"]) for d in dumps)
    safe = re.sub(r'[<>:"/\\|?*]', "", channel or "Channel").strip() or "Channel"
    csv_dir = args.csv_dir or args.out_dir
    os.makedirs(csv_dir, exist_ok=True)
    json_path = os.path.join(csv_dir, f"{safe} Comments.json")
    write_json(json_path, channel, url, dumps)
    csv_path = os.path.join(csv_dir, f"{safe} Comments.csv")
    write_csv(csv_path, dumps)

    log(f"\nSUMMARY: videos={len(dumps)} comments={total} fetched={fetched} "
        f"cached={cached} failed={failed}")
    log(f"JSON saved: {json_path}")
    log(f"CSV saved: {csv_path}")


if __name__ == "__main__":
    main()
