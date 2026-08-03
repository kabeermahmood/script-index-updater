"""Script Index Updater - modern GUI front-end for the index-scripts skill.

A pywebview (Edge WebView2) window hosting index.html. The backend runs
Claude Code headlessly to extract tabs from one or more script PDFs,
identify vehicles, and append new entries to the Word master list.

Launch:  pythonw app.py [pdf1] [pdf2] ...
(Dragging PDFs onto the desktop launcher .bat passes them as arguments.)
"""
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import webview

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_MD = os.path.normpath(os.path.join(HERE, "..", "SKILL.md"))
CONFIG = os.path.join(HERE, "config.json")
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,Task,TodoWrite,Skill"


def load_config():
    try:
        with open(CONFIG, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def default_master():
    """Last-used master list path (config.json), or a sensible default."""
    m = load_config().get("master", "")
    return m or os.path.join(os.path.expanduser("~"), "Documents", "Script Index.docx")


def remember_settings(**settings):
    """Merge the given keys into config.json, preserving the others."""
    cfg = load_config()
    cfg.update(settings)
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=1)
    except OSError:
        pass

PROMPT_TEMPLATE = (
    'Read the file "{skill}" and follow its workflow to index video scripts into the '
    "Word master list specified for each source below.\n{sources}\n"
    "Work fully autonomously - never ask questions; make sensible decisions yourself. "
    "When finished, print a combined summary in markdown: scripts/videos found per source, "
    "how many were already in the master, how many were appended (list each new title with "
    "its hero - the ship/vehicle/aircraft/weapon the script is about), and any anomalies: "
    "things the user should fix in their Google Docs, or videos skipped because they have "
    "no captions."
)

PDF_SOURCE = (
    'Process these PDFs IN ORDER into the master list at "{master}", completing the full '
    "workflow (extract, compare, identify each script's hero, append) for each one before "
    "starting the next, so later PDFs are deduplicated against entries appended from "
    "earlier ones:\n{pdf_list}"
)

CHANNEL_SOURCE = (
    "Index the YouTube channel \"{url}\" into the master list at \"{master}\", following "
    "the skill's channel mode. The transcripts are ALREADY FETCHED: \"{tabs}\" is the "
    "channel's tabs.json and the transcripts folder sits next to it. Do NOT run "
    "fetch_channel.py again - continue from Step 2 (compare / identify / append) on "
    "that tabs.json."
)

PROGRESS_CLAUSE = (
    "\n\nProgress reporting: the moment you begin each major phase of work, print a plain "
    "text line exactly of the form 'PHASE k/N - <label, under 8 words>' where k is the "
    "phase number and N the total phases you plan for the whole job (e.g. extract, "
    "compare, identify vehicles, append - per source). Keep N consistent for the entire "
    "run and emit the phases in order."
)

PHASE_RE = re.compile(r"^PHASE\s+(\d+)\s*/\s*(\d+)\s*[-—:]\s*(.+?)\s*$", re.M)

POLISH_PROMPT = (
    'Read the transcript file "{src}" and write a cleaned-up version of it to '
    '"{dst}".\n\n'
    "It is the caption transcript of one YouTube video. Clean it up in these ways "
    "and no others:\n"
    "- Restore sentence punctuation and capitalisation wherever they are missing.\n"
    "- Break the text into readable paragraphs at natural shifts in the narration.\n"
    "- Correct words the caption engine clearly mis-heard, above all proper nouns: "
    "ship, vehicle, aircraft and weapon designations, place names and people's "
    'names (e.g. "bismark" -> "Bismarck", "you boat" -> "U-boat").\n'
    "- Drop speech-recognition artefacts such as accidentally duplicated words.\n\n"
    "ABSOLUTE RULES - breaking any one of these ruins the result:\n"
    "- NEVER summarise, condense, paraphrase or reword. Every sentence of the "
    "source must appear in the output, saying the same thing in the same words.\n"
    "- NEVER add a preamble, heading, commentary or closing note. The file must "
    "hold the transcript text and nothing else.\n"
    "- The output must be as long as the input: the source has {words} words, so "
    "the output must have at least {floor}.\n"
    "- If the transcript is long, work through it in sequential chunks, appending "
    "each cleaned chunk to the output file, until the WHOLE source is covered. "
    "Never stop early and never skip a passage.\n\n"
    "Print exactly POLISH COMPLETE once the entire transcript has been written."
)

CORRECT_PROMPT = (
    'Read the transcript file "{src}". It is the caption transcript of one '
    "YouTube video, and its punctuation is already fine, so do NOT rewrite it.\n\n"
    "Your only job is to spot words the caption engine mis-heard - above all "
    "proper nouns: ship, vehicle, aircraft and weapon designations, place names, "
    'people\'s names and military terms (e.g. "bismark" -> "Bismarck", '
    '"you boat" -> "U-boat", "Ark Royale" -> "Ark Royal").\n\n'
    'Write a JSON array to "{dst}", at most 60 entries, of the form\n'
    '[{{"wrong": "<text exactly as it appears>", "right": "<correction>"}}]\n\n'
    "Rules:\n"
    '- "wrong" must appear VERBATIM in the file and be at most 5 words long.\n'
    "- Include only genuine mis-transcriptions you are confident about. If there "
    "are none, write [].\n"
    "- Never include changes of wording, grammar, style or punctuation.\n\n"
    "Print DONE once the file is written."
)

POLISH_FLOOR = 0.85  # polished text shorter than this fraction of raw is rejected
MAX_FIX_WORDS = 5    # a "correction" longer than this is a rewrite, not a fix

ANGLE_CLAUSE = (
    "\n\nThe user enabled the Story Context column. For EVERY new entry, also write an "
    '"angle" field in rows.json: 2-3 sentences (~40-60 words) of story context - what '
    "actually happens in the script, its hook, and why it matters. Never a generic "
    "encyclopedia description of the hero. append_master.py automatically adds and "
    "fills this column (existing rows keep an empty cell)."
)


class Api:
    def __init__(self, initial_pdfs):
        self._window = None
        self._proc = None
        self._cancelled = False
        self._paused = False
        self._initial_pdfs = initial_pdfs
        self._pbase, self._pspan = 0, 100  # Claude's slice of the progress bar
        self._master = ""
        self._report_name = ""

    # ---------- helpers ----------
    def _emit(self, **payload):
        try:
            self._window.evaluate_js(f"onEvent({json.dumps(payload)})")
        except Exception:
            pass

    # ---------- exposed to JS ----------
    def defaults(self):
        cfg = load_config()
        return {"master": default_master(),
                "include_angle": bool(cfg.get("include_angle")),
                "comments_top": bool(cfg.get("comments_top")),
                "polish": cfg.get("polish", True),
                "channel": cfg.get("channel", ""),
                "video": cfg.get("video", ""),
                "claude": bool(shutil.which("claude")),
                "initial_pdfs": self._initial_pdfs}

    def pick_pdfs(self):
        res = self._window.create_file_dialog(
            webview.OPEN_DIALOG, allow_multiple=True,
            file_types=("PDF files (*.pdf)",))
        return [str(p) for p in res] if res else []

    def pick_master(self):
        res = self._window.create_file_dialog(
            webview.SAVE_DIALOG, save_filename="Script Index.docx",
            file_types=("Word documents (*.docx)",))
        if isinstance(res, (list, tuple)):
            res = res[0] if res else ""
        return str(res) if res else ""

    def open_path(self, p):
        p = (p or "").strip().strip('"')
        if os.path.exists(p):
            os.startfile(p)

    def copy_text(self, text):
        """Put text on the clipboard. Done here rather than in JS because
        navigator.clipboard is unavailable on WebView2's file:// origin."""
        if os.name != "nt" or not text:
            return False
        try:
            import ctypes
            from ctypes import wintypes
            CF_UNICODETEXT, GMEM_MOVEABLE = 13, 0x0002
            u32, k32 = ctypes.windll.user32, ctypes.windll.kernel32
            k32.GlobalAlloc.restype = wintypes.HGLOBAL
            k32.GlobalLock.restype = ctypes.c_void_p
            k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
            u32.SetClipboardData.restype = wintypes.HANDLE
            u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
            buf = ctypes.create_unicode_buffer(text)
            size = ctypes.sizeof(buf)
            handle = k32.GlobalAlloc(GMEM_MOVEABLE, size)
            if not handle:
                return False
            ctypes.memmove(k32.GlobalLock(handle), buf, size)
            k32.GlobalUnlock(handle)
            if not u32.OpenClipboard(None):
                k32.GlobalFree(handle)
                return False
            try:
                u32.EmptyClipboard()
                if not u32.SetClipboardData(CF_UNICODETEXT, handle):
                    k32.GlobalFree(handle)
                    return False
            finally:
                u32.CloseClipboard()
            return True  # on success the clipboard owns the memory - don't free
        except Exception:
            return False

    def list_channel_videos(self, channel):
        """Enumerate a channel's videos for the picker (no transcripts fetched).
        Returns the videos.json payload, or {"error": …}."""
        channel = (channel or "").strip().strip('"')
        if not channel:
            return {"error": "Enter a YouTube channel URL first."}
        if not re.search(r"(youtube\.com|youtu\.be)/\S", channel, re.I):
            return {"error": f"That doesn't look like a YouTube channel URL: {channel}"}
        if importlib.util.find_spec("yt_dlp") is None:
            return {"error": "Listing videos needs yt-dlp. Run: pip install yt-dlp"}
        out_dir = self._channel_dir(channel)
        script = os.path.normpath(os.path.join(HERE, "..", "scripts", "fetch_channel.py"))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            # deliberately not self._proc: this must not collide with pause/end
            proc = subprocess.run(
                [sys.executable, "-u", script, channel, out_dir, "--list-only"],
                stdin=subprocess.DEVNULL, capture_output=True, text=True,
                encoding="utf-8", errors="replace", creationflags=flags,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"}, timeout=600)
        except subprocess.TimeoutExpired:
            return {"error": "Listing the channel's videos timed out."}
        except Exception as e:
            return {"error": f"Could not list the channel's videos: {e}"}
        path = os.path.join(out_dir, "videos.json")
        if proc.returncode != 0 or not os.path.exists(path):
            tail = [ln for ln in (proc.stdout or "").splitlines() if ln.strip()]
            return {"error": tail[-1] if tail else
                    (proc.stderr or "Could not list the channel's videos.").strip()}
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            return {"error": f"Could not read the video list: {e}"}
        remember_settings(channel=channel)
        return data

    def _tree(self):
        """psutil handles for the worker process and all of its children."""
        try:
            import psutil
            p = psutil.Process(self._proc.pid)
            return [p] + p.children(recursive=True)
        except Exception:
            return []

    def toggle_pause(self):
        """Suspend/resume the whole worker tree. Returns the new paused state."""
        if not self._proc or self._proc.poll() is not None:
            return False
        tree = self._tree()
        if not tree:
            self._emit(kind="err", text="Pause needs psutil - run: pip install psutil")
            return False
        self._paused = not self._paused
        for p in (tree if self._paused else reversed(tree)):
            try:
                p.suspend() if self._paused else p.resume()
            except Exception:
                pass
        self._emit(kind="meta",
                   text="── MISSION PAUSED ──" if self._paused else "── MISSION RESUMED ──")
        return self._paused

    def cancel(self):
        self._cancelled = True
        if self._paused:
            self.toggle_pause()  # a suspended tree can't exit - resume it first
        if self._proc and self._proc.poll() is None:
            tree = self._tree()
            if tree:
                for p in reversed(tree):  # children before parent
                    try:
                        p.kill()
                    except Exception:
                        pass
            else:
                self._proc.kill()

    def start(self, pdfs, master, include_angle=False, channel=""):
        """Validate and launch. Returns an error string, or None if started."""
        pdfs = [p.strip().strip('"') for p in pdfs]
        master = master.strip().strip('"')
        channel = (channel or "").strip().strip('"')
        if not pdfs and not channel:
            return "Add at least one PDF or a YouTube channel URL first."
        if channel and not re.search(r"(youtube\.com|youtu\.be)/\S", channel, re.I):
            return f"That doesn't look like a YouTube channel URL: {channel}"
        if channel and importlib.util.find_spec("yt_dlp") is None:
            return "Channel mode needs yt-dlp. Run: pip install yt-dlp"
        if not master.lower().endswith(".docx"):
            return "The master list must be a .docx path."
        for p in pdfs:
            if not os.path.exists(p):
                return f"File not found: {p}"
        if not os.path.exists(SKILL_MD):
            return f"Skill not found at {SKILL_MD}"
        claude = shutil.which("claude")
        if not claude:
            return "The 'claude' command is not on PATH. Install Claude Code first."

        remember_settings(master=master, include_angle=bool(include_angle),
                          channel=channel)
        self._cancelled = False
        self._paused = False
        threading.Thread(target=self._run_job,
                         args=(claude, pdfs, master, bool(include_angle), channel),
                         daemon=True).start()
        return None

    def start_comments(self, channel, master="", top_only=False):
        """Validate and launch a comments-extraction run. Returns error or None."""
        channel = (channel or "").strip().strip('"')
        master = (master or "").strip().strip('"')
        if not channel:
            return "Enter a YouTube channel URL first."
        if not re.search(r"(youtube\.com|youtu\.be)/\S", channel, re.I):
            return f"That doesn't look like a YouTube channel URL: {channel}"
        if importlib.util.find_spec("yt_dlp") is None:
            return "Comment extraction needs yt-dlp. Run: pip install yt-dlp"
        csv_dir = os.path.dirname(master) if master else os.path.expanduser("~")
        remember_settings(channel=channel, comments_top=bool(top_only))
        self._cancelled = False
        self._paused = False
        threading.Thread(target=self._run_comments_job,
                         args=(channel, csv_dir, 0, bool(top_only)),
                         daemon=True).start()
        return None

    def _run_comments_job(self, channel, csv_dir, max_videos=0, top_only=False):
        """Stream fetch_comments.py: per-video progress, then the CSV export."""
        try:
            script = os.path.normpath(os.path.join(HERE, "..", "scripts",
                                                   "fetch_comments.py"))
            cmd = [sys.executable, "-u", script, channel,
                   self._channel_dir(channel), "--csv-dir", csv_dir]
            if top_only:
                cmd += ["--max-comments", "100"]
            if max_videos:
                cmd += ["--max", str(max_videos)]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            self._emit(kind="progress", pct=0, label="Listing channel videos")
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags, env=env)
            json_path, csv_path, summary = "", "", ""
            for line in self._proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"\[(\d+)/(\d+)\]", line)
                if m:
                    n, total = int(m.group(1)), int(m.group(2))
                    self._emit(kind="progress", pct=round(100 * n / max(total, 1), 1),
                               label=f"Fetching comments ({n}/{total})")
                    self._emit(kind="tool", text=line)
                elif line.startswith("JSON saved: "):
                    json_path = line[len("JSON saved: "):].strip()
                    self._emit(kind="meta", text=line)
                elif line.startswith("CSV saved: "):
                    csv_path = line[len("CSV saved: "):].strip()
                    self._emit(kind="meta", text=line)
                elif line.startswith("SUMMARY: "):
                    summary = line[len("SUMMARY: "):].strip()
                else:
                    self._emit(kind="meta", text=line)
            code = self._proc.wait()
            ok = code == 0 and not self._cancelled and bool(json_path)
            if ok:
                stats = dict(kv.split("=") for kv in summary.split() if "=" in kv)
                self._emit(kind="progress", pct=100, label="Comments exported")
                self._emit(kind="output", path=json_path, label="Open comments JSON")
                self._emit(kind="result", text=(
                    f"## Comments export complete\n\n"
                    f"- **Videos covered:** {stats.get('videos', '?')} "
                    f"({stats.get('fetched', '?')} fetched, {stats.get('cached', '?')} cached, "
                    f"{stats.get('failed', '?')} failed)\n"
                    f"- **Comments exported:** {stats.get('comments', '?')}\n"
                    f"- **JSON (for AI analysis):** `{json_path}`\n"
                    f"- **CSV (for Excel):** `{csv_path}`"))
            self._emit(kind="done", ok=ok, cancelled=self._cancelled)
        except Exception as e:
            self._emit(kind="err", text=f"ERROR: {e}")
            self._emit(kind="done", ok=False, cancelled=self._cancelled)

    def start_transcript(self, videos, master="", polish=True, label=""):
        """Validate and launch a transcript run over one or more videos.
        `videos` is a URL string or a list of {id,title,url,views} records."""
        master = (master or "").strip().strip('"')
        # The UI sends the list as a JSON string: passing an array of objects
        # straight through the JS bridge is not reliable, while strings are.
        if isinstance(videos, str):
            text = videos.strip()
            if text.startswith("["):
                try:
                    videos = json.loads(text)
                except ValueError:
                    return "Could not read the selected videos."
            else:
                videos = [text] if text else []
        if not isinstance(videos, list) or not videos:
            return "Paste a video link, or tick some videos in the picker."
        refs = []
        for v in videos:
            if isinstance(v, str):
                v = {"url": v}
            url = str(v.get("url") or v.get("id") or "").strip().strip('"')
            if not url:
                continue
            if re.search(r"youtube\.com/(@|c/|user/|channel/)", url, re.I) \
                    and not re.search(r"[?&]v=", url, re.I):
                return ("That's a channel link, not a video - use "
                        "“Browse channel videos” to pick one.")
            if not (re.search(r"(youtube\.com|youtu\.be)/\S", url, re.I)
                    or re.match(r"^[A-Za-z0-9_-]{11}$", url)):
                return f"That doesn't look like a YouTube video link: {url}"
            refs.append({"url": url, "id": str(v.get("id") or ""),
                         "title": str(v.get("title") or "").strip(),
                         "views": v.get("views")})
        if not refs:
            return "Paste a video link, or tick some videos in the picker."
        if importlib.util.find_spec("yt_dlp") is None:
            return "Transcript fetching needs yt-dlp. Run: pip install yt-dlp"
        base = (os.path.dirname(master) if master
                else os.path.join(os.path.expanduser("~"), "Documents"))
        out_dir = os.path.join(base or ".", "Transcripts")
        remember_settings(video=refs[0]["url"] if len(refs) == 1 else "",
                          polish=bool(polish))
        # polish needs Claude Code; without it the run still produces a transcript.
        # The warning is raised inside the job, because the UI clears the feed
        # right after this call returns and would wipe anything emitted here.
        effective = bool(polish) and bool(shutil.which("claude"))
        self._cancelled = False
        self._paused = False
        threading.Thread(target=self._run_transcript_job,
                         args=(refs, out_dir, effective,
                               bool(polish) and not effective,
                               str(label or "").strip()), daemon=True).start()
        return None

    def _run_transcript_job(self, refs, out_dir, polish, no_claude=False, label=""):
        """Stage 1: fetch + reflow every transcript into one file.
        Stage 2: optional AI polish over the result."""
        try:
            if no_claude:
                self._emit(kind="err",
                           text="'claude' is not on PATH - skipping the AI polish.")
            script = os.path.normpath(os.path.join(HERE, "..", "scripts",
                                                   "fetch_video.py"))
            batch = os.path.join(tempfile.gettempdir(), "index-scripts",
                                 "_batch.json")
            os.makedirs(os.path.dirname(batch), exist_ok=True)
            with open(batch, "w", encoding="utf-8") as f:
                json.dump(refs, f, ensure_ascii=False)
            cmd = [sys.executable, "-u", script, out_dir, "--batch", batch]
            if label:
                cmd += ["--label", label]
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
            self._emit(kind="progress", pct=4, label=(
                "Fetching transcript" if len(refs) == 1
                else f"Fetching {len(refs)} transcripts"))
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags, env=env)
            title, path, summary = "", "", ""
            for line in self._proc.stdout:
                line = line.rstrip()
                if not line:
                    continue
                m = re.match(r"\[(\d+)/(\d+)\]", line)
                if m:
                    n, total = int(m.group(1)), int(m.group(2))
                    self._emit(kind="progress",
                               pct=round(4 + 51 * n / max(total, 1), 1),
                               label=f"Fetching transcripts ({n}/{total})")
                    self._emit(kind="tool", text=line)
                elif line.startswith("ANOMALY: "):
                    self._emit(kind="err", text=line[len("ANOMALY: "):].strip())
                elif line.startswith("TITLE: "):
                    title = line[len("TITLE: "):].strip()
                    self._emit(kind="meta", text=line)
                elif line.startswith("SAVED: "):
                    path = line[len("SAVED: "):].strip()
                elif line.startswith("SUMMARY: "):
                    summary = line[len("SUMMARY: "):].strip()
                elif line.startswith("ERROR: "):
                    self._emit(kind="err", text=line)
                else:
                    self._emit(kind="meta", text=line)
            code = self._proc.wait()
            if self._cancelled or code != 0 or not path or not os.path.exists(path):
                if not self._cancelled and code != 0:
                    self._emit(kind="err",
                               text="Transcript fetch failed - see the feed above.")
                self._emit(kind="done", ok=False, cancelled=self._cancelled)
                return

            stats = dict(kv.split("=", 1) for kv in summary.split() if "=" in kv)
            with open(path, encoding="utf-8") as f:
                text = f.read()
            raw_words = len(text.split())
            self._emit(kind="progress", pct=55,
                       label="Transcript ready" if not polish else "Cleaning up text")
            polished = False
            if polish and not self._cancelled:
                if stats.get("punctuated") == "yes":
                    self._emit(kind="meta", text=(
                        "These captions already carry punctuation - checking for "
                        "mis-heard names instead of rewriting the text."))
                    new_text, why, applied = self._correct_transcript(path, text)
                    for fix in applied:
                        self._emit(kind="tool", text=f"fixed: {fix}")
                    if new_text is not None and not applied:
                        self._emit(kind="meta", text="No mis-heard names found.")
                else:
                    new_text, why = self._polish_transcript(path, raw_words)
                if new_text:
                    text, polished = new_text, True
                    with open(path, "w", encoding="utf-8") as f:
                        f.write(text)
                elif why:
                    self._emit(kind="err", text=why)
            if self._cancelled:
                self._emit(kind="done", ok=False, cancelled=True)
                return

            self._emit(kind="progress", pct=100, label="Transcript ready")
            self._emit(kind="output", path=path, label="Open transcript")
            self._emit(kind="transcript", title=title, text=text, path=path,
                       words=len(text.split()), raw_words=raw_words,
                       polished=polished, captions=stats.get("kind", ""),
                       cached=stats.get("cached") == "yes",
                       videos=int(stats.get("videos", 1) or 1),
                       failed=int(stats.get("failed", 0) or 0))
            self._emit(kind="done", ok=True, cancelled=False)
        except Exception as e:
            self._emit(kind="err", text=f"ERROR: {e}")
            self._emit(kind="done", ok=False, cancelled=self._cancelled)

    def _claude_pass(self, prompt, dst, label, start_pct=60):
        """Run one headless Claude pass that must leave its output in `dst`.
        Returns (ok, reason)."""
        claude = shutil.which("claude")
        if not claude:
            return False, "Claude Code is not on PATH - keeping the raw transcript."
        try:
            if os.path.exists(dst):  # never judge a run by a previous one's output
                os.remove(dst)
        except OSError:
            pass
        self._emit(kind="progress", pct=start_pct, label=label)
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            self._proc = subprocess.Popen(
                [claude, "-p", "--output-format", "stream-json", "--verbose",
                 "--allowedTools", "Read,Write,Edit"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags, cwd=os.path.expanduser("~"))
            try:
                self._proc.stdin.write(prompt)
            finally:
                self._proc.stdin.close()
            steps = 0
            for line in self._proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except ValueError:
                    continue
                if evt.get("type") != "assistant":
                    continue
                for block in evt.get("message", {}).get("content", []):
                    if block.get("type") == "tool_use":
                        steps += 1
                        self._emit(kind="tool",
                                   text=f"{block.get('name', 'tool')}: {label.lower()}")
                        self._emit(kind="progress",
                                   pct=min(92, start_pct + steps * 4), label=label)
            code = self._proc.wait()
        except Exception as e:
            return False, f"The AI pass failed ({e}) - keeping the raw transcript."
        if self._cancelled:
            return False, None
        if code != 0 or not os.path.exists(dst):
            return False, "The AI pass failed - keeping the raw transcript."
        return True, None

    def _correct_transcript(self, path, text):
        """Fast path for captions that already carry punctuation: Claude returns
        a short list of mis-heard words and they are applied here. Rewriting the
        whole transcript would spend minutes regenerating text that is already
        correct, and applying edits locally means it can never be truncated.
        Returns (text, reason, applied_fixes)."""
        dst = os.path.splitext(path)[0] + ".fixes.json"
        ok, why = self._claude_pass(CORRECT_PROMPT.format(src=path, dst=dst),
                                    dst, "Checking names")
        if not ok:
            return None, why, []
        try:
            with open(dst, encoding="utf-8") as f:
                pairs = json.load(f)
        except (OSError, ValueError) as e:
            return None, f"Could not read the corrections ({e}) - keeping the raw text.", []
        finally:
            try:
                os.remove(dst)
            except OSError:
                pass
        if not isinstance(pairs, list):
            return None, "The corrections were not a list - keeping the raw text.", []
        out, applied = text, []
        for p in pairs[:60]:
            if not isinstance(p, dict):
                continue
            wrong = str(p.get("wrong", "")).strip()
            right = str(p.get("right", "")).strip()
            if (not wrong or not right or wrong == right
                    or len(wrong.split()) > MAX_FIX_WORDS or len(wrong) > 60):
                continue  # a long "correction" is a rewrite in disguise
            # lambda replacement: backslashes and \1 in `right` stay literal
            new, n = re.subn(rf"(?<!\w){re.escape(wrong)}(?!\w)", lambda m: right, out)
            if n:
                out = new
                applied.append(f"{wrong} → {right} ({n}×)")
        ratio = len(out.split()) / max(len(text.split()), 1)
        if not 0.9 <= ratio <= 1.1:
            return None, "The corrections changed the length too much - keeping the raw text.", []
        return out, None, applied

    def _polish_transcript(self, path, raw_words):
        """Full rewrite, for captions that arrive with no punctuation at all.
        Anything that loses a meaningful chunk of the text is rejected - a
        truncated transcript is worse than a rough one."""
        dst = os.path.splitext(path)[0] + ".polished.txt"
        floor = max(1, int(raw_words * POLISH_FLOOR))
        ok, why = self._claude_pass(
            POLISH_PROMPT.format(src=path, dst=dst, words=raw_words, floor=floor),
            dst, "Polishing transcript")
        if not ok:
            return None, why
        try:
            with open(dst, encoding="utf-8") as f:
                out = f.read().strip()
        except OSError as e:
            return None, f"Could not read the polished text ({e}) - keeping the raw one."
        finally:
            try:
                os.remove(dst)
            except OSError:
                pass
        got = len(out.split())
        if got < floor:
            return None, (f"Polish returned only {got} of {raw_words} words, so it was "
                          "discarded - keeping the raw transcript.")
        return out, None

    # ---------- worker ----------
    @staticmethod
    def _channel_dir(url):
        slug = hashlib.md5(url.lower().encode()).hexdigest()[:10]
        return os.path.join(tempfile.gettempdir(), "index-scripts", f"channel-{slug}")

    def _run_job(self, claude, pdfs, master, include_angle, channel):
        """Stage 1: fetch channel transcripts (live per-video progress).
        Stage 2: headless Claude run over PDFs and/or the fetched channel."""
        try:
            self._master = master
            self._report_name = ""
            chan_tabs, chan_master = None, master
            if channel:
                fetch_span = (0, 50) if pdfs else (0, 60)
                if not self._fetch_channel(channel, fetch_span):
                    self._emit(kind="done", ok=False, cancelled=self._cancelled)
                    return
                chan_tabs = os.path.join(self._channel_dir(channel), "tabs.json")
                try:
                    with open(chan_tabs, encoding="utf-8") as f:
                        self._report_name = (json.load(f).get("channel") or "").strip()
                except (OSError, ValueError):
                    pass
                if self._report_name:
                    # every channel gets its own index, named after the channel
                    safe = re.sub(r'[<>:"/\\|?*]', "", self._report_name).strip()
                    chan_master = os.path.join(os.path.dirname(master) or ".",
                                               f"{safe} Script Index.docx")
                    self._master = chan_master
                    self._emit(kind="meta", text=f"Channel index: {chan_master}")
                    self._emit(kind="master", path=chan_master)
                self._pbase = fetch_span[1]
                self._pspan = 100 - self._pbase
            else:
                self._pbase, self._pspan = 0, 100
            if not self._report_name:  # PDF-only run: name after the master list
                self._report_name = os.path.splitext(os.path.basename(master))[0]

            sources = []
            if pdfs:
                pdf_list = "\n".join(f'{i + 1}. "{p}"' for i, p in enumerate(pdfs))
                sources.append(PDF_SOURCE.format(master=master, pdf_list=pdf_list))
            if channel:
                sources.append(CHANNEL_SOURCE.format(url=channel, master=chan_master,
                                                     tabs=chan_tabs))
            prompt = PROMPT_TEMPLATE.format(skill=SKILL_MD,
                                            sources="\n\n".join(sources))
            if include_angle:
                prompt += ANGLE_CLAUSE
            prompt += PROGRESS_CLAUSE
            self._emit(kind="progress", pct=self._pbase, label="Analyzing scripts")
            # The prompt goes via stdin: on Windows the claude CLI is a .cmd
            # shim, and cmd.exe truncates argv at the first newline, silently
            # dropping most of the prompt.
            cmd = [claude, "-p",
                   "--output-format", "stream-json", "--verbose",
                   "--allowedTools", ALLOWED_TOOLS]
            self._run(cmd, prompt)
        except Exception as e:
            self._emit(kind="err", text=f"ERROR: {e}")
            self._emit(kind="done", ok=False, cancelled=self._cancelled)

    def _fetch_channel(self, url, span, max_videos=0):
        """Run fetch_channel.py, streaming its per-video progress to the UI."""
        out_dir = self._channel_dir(url)
        script = os.path.normpath(os.path.join(HERE, "..", "scripts", "fetch_channel.py"))
        cmd = [sys.executable, "-u", script, url, out_dir]
        if max_videos:
            cmd += ["--max", str(max_videos)]
        self._emit(kind="progress", pct=span[0], label="Listing channel videos")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
        self._proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", creationflags=flags, env=env)
        for line in self._proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            m = re.match(r"\[(\d+)/(\d+)\]", line)
            if m:
                n, total = int(m.group(1)), int(m.group(2))
                pct = span[0] + (span[1] - span[0]) * n / max(total, 1)
                self._emit(kind="progress", pct=round(pct, 1),
                           label=f"Fetching transcripts ({n}/{total})")
                self._emit(kind="tool", text=line)
            else:
                self._emit(kind="meta", text=line)
        code = self._proc.wait()
        if self._cancelled or code != 0:
            if not self._cancelled:
                self._emit(kind="err", text="Transcript fetch failed - see feed above.")
            return False
        self._emit(kind="progress", pct=span[1], label="Transcripts ready")
        return True

    def _run(self, cmd, prompt=None):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE if prompt else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags,
                cwd=os.path.expanduser("~"))
            if prompt:
                try:
                    self._proc.stdin.write(prompt)
                finally:
                    self._proc.stdin.close()
            got_result = False
            for line in self._proc.stdout:
                got_result = self._handle_line(line) or got_result
            code = self._proc.wait()
            ok = code == 0 and got_result and not self._cancelled
            self._emit(kind="done", ok=ok, cancelled=self._cancelled)
        except Exception as e:
            self._emit(kind="err", text=f"ERROR: {e}")
            self._emit(kind="done", ok=False, cancelled=False)

    def _handle_line(self, line):
        line = line.strip()
        if not line:
            return False
        try:
            evt = json.loads(line)
        except ValueError:
            self._emit(kind="meta", text=line)
            return False
        etype = evt.get("type")
        if etype == "assistant":
            for block in evt.get("message", {}).get("content", []):
                btype = block.get("type")
                if btype == "text" and block.get("text", "").strip():
                    text = block["text"].strip()
                    for m in PHASE_RE.finditer(text):
                        k, n = int(m.group(1)), max(int(m.group(2)), 1)
                        pct = self._pbase + self._pspan * (min(k, n) - 1) / n
                        self._emit(kind="progress", pct=round(pct, 1),
                                   label=m.group(3))
                    self._emit(kind="say", text=text)
                elif btype == "tool_use":
                    name = block.get("name", "tool")
                    inp = block.get("input", {})
                    detail = ""
                    if name == "Bash":
                        detail = inp.get("description") or ""
                    elif name in ("Read", "Write", "Edit"):
                        detail = os.path.basename(str(inp.get("file_path", "")))
                    elif name == "Task":
                        detail = inp.get("description") or ""
                    self._emit(kind="tool", text=f"{name}{': ' + detail if detail else ''}")
        elif etype == "result":
            text = evt.get("result")
            if text:
                self._save_report(text.strip())
                self._emit(kind="result", text=text.strip())
                return True
        return False

    def _save_report(self, text):
        """Persist the mission report, named after the channel (or master list)."""
        try:
            rdir = os.path.join(os.path.dirname(self._master) or ".", "Mission Reports")
            os.makedirs(rdir, exist_ok=True)
            name = re.sub(r'[<>:"/\\|?*]', "", self._report_name).strip() or "Run"
            stamp = time.strftime("%Y-%m-%d %H.%M")
            path = os.path.join(rdir, f"{name} - {stamp}.md")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            self._emit(kind="meta", text=f"Mission report saved: {path}")
        except OSError as e:
            self._emit(kind="err", text=f"Could not save the report: {e}")


def apply_window_icon(window):
    """Set the title-bar and taskbar icon (Windows only)."""
    if os.name != "nt":
        return
    try:
        import ctypes
        ico = os.path.normpath(os.path.join(HERE, "..", "assets", "icon.ico"))
        if not os.path.exists(ico):
            return
        hwnd = int(window.native.Handle.ToInt64())
        IMAGE_ICON, LR_LOADFROMFILE, WM_SETICON = 1, 0x10, 0x80
        for size, which in ((16, 0), (48, 1)):
            h = ctypes.windll.user32.LoadImageW(
                None, ico, IMAGE_ICON, size, size, LR_LOADFROMFILE)
            if h:
                ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, which, h)
    except Exception:
        pass


def main():
    if os.name == "nt":
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
                "TalhaHamdees.ScriptIndexUpdater")
        except Exception:
            pass
    initial = [a.strip().strip('"') for a in sys.argv[1:]
               if a.lower().strip().strip('"').endswith(".pdf")]
    api = Api(initial)
    window = webview.create_window(
        "Script Index Updater",
        os.path.join(HERE, "index.html"),
        js_api=api,
        width=1000, height=760, min_size=(820, 600),
        background_color="#0b0f14")
    api._window = window
    window.events.shown += lambda *a: apply_window_icon(window)
    webview.start()


if __name__ == "__main__":
    main()
