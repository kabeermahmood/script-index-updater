"""Script Index Updater - modern GUI front-end for the index-scripts skill.

A pywebview (Edge WebView2) window hosting index.html. The backend runs
Claude Code headlessly to extract tabs from one or more script PDFs,
identify vehicles, and append new entries to the Word master list.

Launch:  pythonw app.py [pdf1] [pdf2] ...
(Dragging PDFs onto the desktop launcher .bat passes them as arguments.)
"""
import json
import os
import shutil
import subprocess
import sys
import threading

import webview

HERE = os.path.dirname(os.path.abspath(__file__))
SKILL_MD = os.path.normpath(os.path.join(HERE, "..", "SKILL.md"))
CONFIG = os.path.join(HERE, "config.json")
ALLOWED_TOOLS = "Read,Write,Edit,Glob,Grep,Bash,Task,TodoWrite,Skill"


def default_master():
    """Last-used master list path (config.json), or a sensible default."""
    try:
        with open(CONFIG, encoding="utf-8") as f:
            m = json.load(f).get("master", "")
            if m:
                return m
    except (OSError, ValueError):
        pass
    return os.path.join(os.path.expanduser("~"), "Documents", "Script Index.docx")


def remember_master(master):
    try:
        with open(CONFIG, "w", encoding="utf-8") as f:
            json.dump({"master": master}, f, indent=1)
    except OSError:
        pass

PROMPT_TEMPLATE = (
    'Read the file "{skill}" and follow its workflow to index video-script PDFs into the '
    'master Word list at "{master}". Process these PDFs IN ORDER, completing the full '
    "workflow (extract, compare, identify vehicles, append) for each one before starting "
    "the next, so later PDFs are deduplicated against entries appended from earlier ones:\n"
    "{pdf_list}\n"
    "Work fully autonomously - never ask questions; make sensible decisions yourself. "
    "When finished, print a combined summary in markdown: tabs found per PDF, how many "
    "were already in the master, how many were appended (list each new title with its "
    "vehicle), and any anomalies the user should fix in their Google Docs."
)


class Api:
    def __init__(self, initial_pdfs):
        self._window = None
        self._proc = None
        self._cancelled = False
        self._initial_pdfs = initial_pdfs

    # ---------- helpers ----------
    def _emit(self, **payload):
        try:
            self._window.evaluate_js(f"onEvent({json.dumps(payload)})")
        except Exception:
            pass

    # ---------- exposed to JS ----------
    def defaults(self):
        return {"master": default_master(),
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

    def start(self, pdfs, master):
        """Validate and launch. Returns an error string, or None if started."""
        pdfs = [p.strip().strip('"') for p in pdfs]
        master = master.strip().strip('"')
        for p in pdfs:
            if not os.path.exists(p):
                return f"File not found: {p}"
        if not os.path.exists(SKILL_MD):
            return f"Skill not found at {SKILL_MD}"
        claude = shutil.which("claude")
        if not claude:
            return "The 'claude' command is not on PATH. Install Claude Code first."

        remember_master(master)
        pdf_list = "\n".join(f'{i + 1}. "{p}"' for i, p in enumerate(pdfs))
        prompt = PROMPT_TEMPLATE.format(skill=SKILL_MD, master=master, pdf_list=pdf_list)
        cmd = [claude, "-p", prompt,
               "--output-format", "stream-json", "--verbose",
               "--allowedTools", ALLOWED_TOOLS]
        self._cancelled = False
        threading.Thread(target=self._run, args=(cmd,), daemon=True).start()
        return None

    # ---------- worker ----------
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
                    self._emit(kind="say", text=block["text"].strip())
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


def main():
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
    webview.start()


if __name__ == "__main__":
    main()
