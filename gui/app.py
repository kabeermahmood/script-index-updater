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


def remember_settings(master, include_angle, channel=""):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump({"master": master, "include_angle": include_angle,
                       "channel": channel}, f, indent=1)
    except OSError:
        pass

PROMPT_TEMPLATE = (
    'Read the file "{skill}" and follow its workflow to index video scripts into the '
    'master Word list at "{master}".\n{sources}\n'
    "Work fully autonomously - never ask questions; make sensible decisions yourself. "
    "When finished, print a combined summary in markdown: scripts/videos found per source, "
    "how many were already in the master, how many were appended (list each new title with "
    "its vehicle), and any anomalies: things the user should fix in their Google Docs, or "
    "videos skipped because they have no captions."
)

PDF_SOURCE = (
    "Process these PDFs IN ORDER, completing the full workflow (extract, compare, identify "
    "vehicles, append) for each one before starting the next, so later PDFs are "
    "deduplicated against entries appended from earlier ones:\n{pdf_list}"
)

CHANNEL_SOURCE = (
    "Index the YouTube channel \"{url}\" following the skill's channel mode. The "
    "transcripts are ALREADY FETCHED: \"{tabs}\" is the channel's tabs.json and the "
    "transcripts folder sits next to it. Do NOT run fetch_channel.py again - continue "
    "from Step 2 (compare / identify / append) on that tabs.json."
)

PROGRESS_CLAUSE = (
    "\n\nProgress reporting: the moment you begin each major phase of work, print a plain "
    "text line exactly of the form 'PHASE k/N - <label, under 8 words>' where k is the "
    "phase number and N the total phases you plan for the whole job (e.g. extract, "
    "compare, identify vehicles, append - per source). Keep N consistent for the entire "
    "run and emit the phases in order."
)

PHASE_RE = re.compile(r"^PHASE\s+(\d+)\s*/\s*(\d+)\s*[-—:]\s*(.+?)\s*$", re.M)

ANGLE_CLAUSE = (
    "\n\nThe user enabled the Angle/Summary column. For EVERY new entry, also write an "
    '"angle" field in rows.json: a concise 1-2 sentence summary (max ~30 words) of the '
    "script's specific angle or hook - the particular story it tells about the vehicle, "
    "not a generic description of the vehicle itself. append_master.py automatically "
    "adds and fills the 'Angle / Summary' column (existing rows keep an empty cell)."
)


class Api:
    def __init__(self, initial_pdfs):
        self._window = None
        self._proc = None
        self._cancelled = False
        self._initial_pdfs = initial_pdfs
        self._pbase, self._pspan = 0, 100  # Claude's slice of the progress bar

    # ---------- helpers ----------
    def _emit(self, **payload):
        try:
            self._window.evaluate_js(f"onEvent({json.dumps(payload)})")
        except Exception:
            pass

    # ---------- exposed to JS ----------
    def defaults(self):
        return {"master": default_master(),
                "include_angle": bool(load_config().get("include_angle")),
                "channel": load_config().get("channel", ""),
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

    def cancel(self):
        self._cancelled = True
        if self._proc and self._proc.poll() is None:
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

        remember_settings(master, bool(include_angle), channel)
        self._cancelled = False
        threading.Thread(target=self._run_job,
                         args=(claude, pdfs, master, bool(include_angle), channel),
                         daemon=True).start()
        return None

    # ---------- worker ----------
    @staticmethod
    def _channel_dir(url):
        slug = hashlib.md5(url.lower().encode()).hexdigest()[:10]
        return os.path.join(tempfile.gettempdir(), "index-scripts", f"channel-{slug}")

    def _run_job(self, claude, pdfs, master, include_angle, channel):
        """Stage 1: fetch channel transcripts (live per-video progress).
        Stage 2: headless Claude run over PDFs and/or the fetched channel."""
        try:
            chan_tabs = None
            if channel:
                fetch_span = (0, 50) if pdfs else (0, 60)
                if not self._fetch_channel(channel, fetch_span):
                    self._emit(kind="done", ok=False, cancelled=self._cancelled)
                    return
                chan_tabs = os.path.join(self._channel_dir(channel), "tabs.json")
                self._pbase = fetch_span[1]
                self._pspan = 100 - self._pbase
            else:
                self._pbase, self._pspan = 0, 100

            sources = []
            if pdfs:
                pdf_list = "\n".join(f'{i + 1}. "{p}"' for i, p in enumerate(pdfs))
                sources.append(PDF_SOURCE.format(pdf_list=pdf_list))
            if channel:
                clause = CHANNEL_SOURCE.format(url=channel, tabs=chan_tabs)
                if pdfs:
                    clause = ("After all PDFs are fully processed and appended: " + clause +
                              " This dedupes the channel's videos against the entries the "
                              "PDFs just added.")
                sources.append(clause)
            prompt = PROMPT_TEMPLATE.format(skill=SKILL_MD, master=master,
                                            sources="\n\n".join(sources))
            if include_angle:
                prompt += ANGLE_CLAUSE
            prompt += PROGRESS_CLAUSE
            self._emit(kind="progress", pct=self._pbase, label="Analyzing scripts")
            cmd = [claude, "-p", prompt,
                   "--output-format", "stream-json", "--verbose",
                   "--allowedTools", ALLOWED_TOOLS]
            self._run(cmd)
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

    def _run(self, cmd):
        try:
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            self._proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", creationflags=flags,
                cwd=os.path.expanduser("~"))
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
                self._emit(kind="result", text=text.strip())
                return True
        return False


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
        width=1000, height=680, min_size=(820, 560),
        background_color="#0b0f14")
    api._window = window
    window.events.shown += lambda *a: apply_window_icon(window)
    webview.start()


if __name__ == "__main__":
    main()
