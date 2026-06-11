"""Fetch every comment from every video on a YouTube channel.

Usage: python fetch_comments.py <channel_url> <out_dir>
           [--csv-dir DIR] [--max N] [--delay SECS] [--max-comments N]

Enumerates all videos on the channel's Videos tab (numbered oldest-first,
same as fetch_channel.py), downloads each video's full comment threads
(top-level comments and replies) via yt-dlp - no API key needed - and writes:

  <out_dir>/comments/<video_id>.json     raw per-video comment dump
  <csv-dir>/<Channel Name> Comments.csv  one spreadsheet with every comment

Per-video dumps are cached on disk, so re-running only fetches comments for
new videos (the CSV is rebuilt from the cache every run, so it always holds
the full channel). Videos with comments disabled get an empty dump and are
counted in the summary. A polite delay separates fetches.

The CSV is UTF-8 with BOM so Excel opens it correctly. Columns:
video #, video id, video title, comment id, parent id, reply?, author,
uploader?, likes, published (UTC), comment text.

Requires: pip install yt-dlp
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time

from fetch_channel import list_videos, log, normalize_channel_url

KEEP = ("id", "parent", "text", "author", "author_is_uploader",
        "like_count", "timestamp")


def fetch_comments(video_url, max_comments):
    from yt_dlp import YoutubeDL
    eargs = {}
    if max_comments > 0:
        eargs = {"youtube": {"max_comments": [str(max_comments)]}}
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
                    help="cap comments per video (0 = all)")
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

    dumps, fetched, cached, failed = [], 0, 0, 0
    for n, v in numbered:
        jpath = os.path.join(cdir, f"{v['id']}.json")
        label = f"[{n}/{len(videos)}] {v['title'][:70]}"
        if os.path.exists(jpath):
            with open(jpath, encoding="utf-8") as f:
                dump = json.load(f)
            cached += 1
            log(f"{label} - cached ({len(dump['comments'])} comments)")
        else:
            try:
                comments = fetch_comments(v["url"], args.max_comments)
            except Exception as e:
                failed += 1
                log(f"{label} - fetch failed: {e} - skipped")
                time.sleep(args.delay)
                continue
            dump = {"n": n, "video_id": v["id"], "title": v["title"],
                    "url": v["url"], "comments": comments}
            with open(jpath, "w", encoding="utf-8") as f:
                json.dump(dump, f, ensure_ascii=False)
            fetched += 1
            log(f"{label} - {len(comments)} comment(s) saved")
            time.sleep(args.delay)
        dump["n"] = n  # keep numbering current even for cached dumps
        dumps.append(dump)

    total = sum(len(d["comments"]) for d in dumps)
    safe = re.sub(r'[<>:"/\\|?*]', "", channel or "Channel").strip() or "Channel"
    csv_dir = args.csv_dir or args.out_dir
    os.makedirs(csv_dir, exist_ok=True)
    csv_path = os.path.join(csv_dir, f"{safe} Comments.csv")
    write_csv(csv_path, dumps)

    log(f"\nSUMMARY: videos={len(dumps)} comments={total} fetched={fetched} "
        f"cached={cached} failed={failed}")
    log(f"CSV saved: {csv_path}")


if __name__ == "__main__":
    main()
