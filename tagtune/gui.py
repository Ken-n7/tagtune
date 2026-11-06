"""Tk window."""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading

from .config import CONFIG, OUT_DIR, _job_dirs, log
from .text import validate_url
from .download import preflight, run_job

PALETTES = {
    "light": dict(bg="#F6F8F6", surface="#FFFFFF", text="#14201A", muted="#5B6B62", border="#D5DDD8",
                  accent="#15803D", hover="#166534", on_accent="#FFFFFF", error="#B91C1C"),
    "dark": dict(bg="#0F1512", surface="#18211C", text="#E6EFE9", muted="#9AA9A0", border="#2A3730",
                 accent="#22C55E", hover="#4ADE80", on_accent="#06210F", error="#F87171"),
}
SYMBOL = {"ok": "✓", "keep": "~", "skip": "=", "fail": "✗"}
HINT = "Downloaded songs will appear here.\n"


def system_theme():
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        return "dark" if "dark" in out else "light"
    except (OSError, subprocess.SubprocessError):
        return "light"


def load_theme():
    try:
        theme = json.loads(CONFIG.read_text()).get("theme")
    except (OSError, ValueError, AttributeError):
        theme = None
    return theme if theme in PALETTES else system_theme()


class App:
    def __init__(self, root):
        import tkinter as tk
        from tkinter import font as tkfont
        from tkinter import ttk

        self.tk, self.ttk, self.root = tk, ttk, root
        self.q = queue.Queue()
        self.cancel = threading.Event()
        self.running = False
        self.updating = False
        self.stale = False
        self.theme = load_theme()
        self.style = ttk.Style(root)
        self.style.theme_use("clam")

        for name in ("TkDefaultFont", "TkTextFont"):
            tkfont.nametofont(name).configure(size=11)
        title_font = tkfont.nametofont("TkDefaultFont").copy()
        title_font.configure(size=18, weight="bold")
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.style.configure("Title.TLabel", font=title_font)
        self.style.configure("Accent.TButton", font=bold)

        root.title("TagTune")
        root.geometry("640x500")
        root.minsize(560, 440)
        f = ttk.Frame(root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(7, weight=1)

        ttk.Label(f, text="TagTune", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        self.theme_btn = ttk.Button(f, command=self.toggle_theme, takefocus=False)
        self.theme_btn.grid(row=0, column=1, sticky="e")

        ttk.Label(f, text="YouTube or YouTube Music link", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(16, 4))
        self.url = tk.StringVar()
        self.entry = ttk.Entry(f, textvariable=self.url)
        self.entry.grid(row=2, column=0, sticky="ew", padx=(0, 8), ipady=4)
        self.go = ttk.Button(f, text="Download", style="Accent.TButton", command=self.start)
        self.go.grid(row=2, column=1, sticky="ew")

        self.exact = tk.BooleanVar(value=False)
        self.exact_box = ttk.Checkbutton(f, variable=self.exact,
                                         text="Download exactly this video (don't look for the official version)")
        self.exact_box.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.msg = ttk.Label(f, style="Error.TLabel", justify="left")
        self.msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.bar = ttk.Progressbar(f, maximum=100)
        self.bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        self.status = ttk.Label(f, text="Ready", style="Muted.TLabel")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w")

        self.list = tk.Text(f, height=8, wrap="word", relief="flat", borderwidth=0, highlightthickness=1,
                            padx=12, pady=8, spacing1=2, spacing3=2, cursor="arrow", takefocus=False,
                            font="TkDefaultFont")
        self.list.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(12, 12))
        self.list.insert("end", HINT, "muted")
        self.list.configure(state="disabled")
        self.empty = True

        foot = ttk.Frame(f)
        foot.grid(row=8, column=0, columnspan=2, sticky="ew")
        foot.columnconfigure(0, weight=1)
        self.update_btn = ttk.Button(foot, text="Update downloader", command=self.update_downloader)
        ttk.Button(foot, text="Open folder", command=self.open_folder).grid(row=0, column=2, sticky="e")

        f.bind("<Configure>", lambda e: self.msg.configure(wraplength=max(200, e.width - 32)))
        root.bind("<Return>", lambda e: self.start())
        root.bind("<Escape>", lambda e: self.stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.apply_theme()

        fatal, warnings = preflight()
        self.blocked = bool(fatal)
        if fatal:
            self.go.state(["disabled"])
        self.show_message(fatal or " ".join(warnings))
        try:
            clip, err = validate_url(root.clipboard_get())
        except tk.TclError:
            clip, err = None, "empty clipboard"
        if clip and not err:
            self.url.set(clip)
            self.entry.select_range(0, "end")
        self.entry.focus_set()
        root.after(100, self.poll)

    # theme

    def apply_theme(self):
        p, s = PALETTES[self.theme], self.style
        self.root.configure(bg=p["bg"])
        s.configure(".", background=p["bg"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["border"], focuscolor=p["accent"])
        s.configure("Muted.TLabel", foreground=p["muted"])
        s.configure("Error.TLabel", foreground=p["error"])
        s.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], insertcolor=p["text"],
                    lightcolor=p["surface"], darkcolor=p["surface"], padding=6,
                    selectbackground=p["accent"], selectforeground=p["on_accent"])
        s.map("TEntry", bordercolor=[("disabled", p["border"]), ("focus", p["accent"])],
              lightcolor=[("disabled", p["bg"]), ("focus", p["accent"])],
              darkcolor=[("disabled", p["bg"]), ("focus", p["accent"])], fieldbackground=[("disabled", p["bg"])],
              foreground=[("disabled", p["muted"])])
        s.configure("TButton", background=p["bg"], foreground=p["text"], padding=(12, 6))
        s.map("TButton", background=[("active", p["surface"])], bordercolor=[("focus", p["accent"])],
              foreground=[("disabled", p["muted"])])
        s.configure("Accent.TButton", background=p["accent"], foreground=p["on_accent"], bordercolor=p["accent"],
                    lightcolor=p["accent"], darkcolor=p["accent"], focuscolor=p["on_accent"], padding=(16, 6))
        s.map("Accent.TButton", background=[("disabled", p["border"]), ("active", p["hover"])],
              foreground=[("disabled", p["muted"])], bordercolor=[("disabled", p["border"])],
              lightcolor=[("disabled", p["border"]), ("active", p["hover"])],
              darkcolor=[("disabled", p["border"]), ("active", p["hover"])])
        s.configure("TCheckbutton", background=p["bg"], foreground=p["text"], indicatorbackground=p["surface"],
                    indicatorforeground=p["on_accent"], upperbordercolor=p["muted"], lowerbordercolor=p["muted"],
                    padding=(0, 2))
        s.map("TCheckbutton", background=[("active", p["bg"])], foreground=[("disabled", p["muted"])],
              indicatorbackground=[("selected", p["accent"]), ("active", p["surface"])],
              upperbordercolor=[("selected", p["accent"]), ("focus", p["accent"])],
              lowerbordercolor=[("selected", p["accent"]), ("focus", p["accent"])])
        s.configure("Horizontal.TProgressbar", background=p["accent"], lightcolor=p["accent"],
                    darkcolor=p["accent"], bordercolor=p["border"], troughcolor=p["border"])
        self.list.configure(background=p["surface"], foreground=p["text"], highlightbackground=p["border"],
                            highlightcolor=p["border"], selectbackground=p["accent"],
                            selectforeground=p["on_accent"], insertbackground=p["surface"])
        for tag, color in (("ok", "accent"), ("keep", "text"), ("skip", "muted"), ("fail", "error"), ("muted", "muted")):
            self.list.tag_configure(tag, foreground=p[color])
        self.theme_btn.configure(text="◐ Light mode" if self.theme == "dark" else "◐ Dark mode")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply_theme()
        try:
            CONFIG.parent.mkdir(parents=True, exist_ok=True)
            CONFIG.write_text(json.dumps({"theme": self.theme}))
        except OSError:
            log.exception("could not save theme")

    # actions

    def show_message(self, text):
        self.msg.configure(text=f"⚠ {text}" if text else "")

    def start(self):
        if self.running or self.blocked or self.updating:
            return
        url, err = validate_url(self.url.get())
        self.show_message(err)
        if err:
            self.entry.focus_set()
            return
        self.set_running(True)
        self.stale = False
        self.update_btn.grid_forget()
        self.cancel.clear()
        emit = lambda *m: self.q.put(m)  # noqa: E731
        threading.Thread(target=run_job, args=(url, emit, self.cancel, self.confirm, self.exact.get()),
                         daemon=True).start()

    def stop(self):
        if self.running:
            self.cancel.set()
            self.status.configure(text="Cancelling…")

    def set_running(self, running):
        self.running = running
        self.entry.state(["disabled" if running else "!disabled"])
        self.exact_box.state(["disabled" if running else "!disabled"])
        self.go.configure(text="Cancel" if running else "Download",
                          style="TButton" if running else "Accent.TButton",
                          command=self.stop if running else self.start)
        self.busy(running)

    def busy(self, on):
        self.bar.stop()
        self.bar.configure(mode="indeterminate" if on else "determinate", value=0)
        if on:
            self.bar.start(15)

    def confirm(self, count):
        """Called from the worker: ask on the UI thread and wait for the answer."""
        reply = queue.Queue()
        self.q.put(("ask", count, reply))
        return reply.get()

    def open_folder(self):
        try:
            OUT_DIR.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(OUT_DIR)])
        except OSError:
            self.show_message(f"Couldn't open {OUT_DIR}.")

    def update_downloader(self):
        if self.running or self.updating:
            return
        self.updating = True
        self.update_btn.state(["disabled"])
        self.go.state(["disabled"])
        self.status.configure(text="Updating downloader…")
        self.busy(True)

        def work():
            try:
                ok = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                                    capture_output=True, timeout=300).returncode == 0
            except (OSError, subprocess.SubprocessError):
                ok = False
            self.q.put(("updated", ok))

        threading.Thread(target=work, daemon=True).start()

    def close(self):
        from tkinter import messagebox

        if self.running and not messagebox.askokcancel(
                "Download in progress", "Stop the download and quit?", parent=self.root):
            return
        self.cancel.set()
        for tmp in list(_job_dirs):
            shutil.rmtree(tmp, ignore_errors=True)
        self.root.destroy()

    # messages from the worker

    def poll(self):
        try:
            while True:
                kind, *args = self.q.get_nowait()
                try:
                    getattr(self, "on_" + kind)(*args)
                except Exception:  # one bad message must not stop the window from listening
                    log.exception("could not handle %s%r", kind, args)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def on_status(self, text):
        if not self.cancel.is_set():
            self.status.configure(text=text)
        if str(self.bar.cget("mode")) != "indeterminate":
            self.busy(True)

    def on_progress(self, pct, text):
        if str(self.bar.cget("mode")) != "determinate":
            self.bar.stop()
            self.bar.configure(mode="determinate")
        self.bar.configure(value=pct)
        if not self.cancel.is_set():
            self.status.configure(text=text)

    def on_row(self, kind, name, note):
        self.list.configure(state="normal")
        if self.empty:
            self.list.delete("1.0", "end")
            self.empty = False
        self.list.insert("end", f"{SYMBOL[kind]}  ", kind, name, "", f"   {note}\n", "fail" if kind == "fail" else "muted")
        self.list.configure(state="disabled")
        self.list.see("end")

    def on_ask(self, count, reply):
        from tkinter import messagebox

        answer = False  # the worker is waiting on this, so it must always get one
        try:
            answer = messagebox.askyesno("Large playlist", f"This playlist has {count} songs. Download all of them?",
                                         parent=self.root)
        finally:
            reply.put(answer)

    def on_stale(self):
        self.stale = True  # the button is offered once the job is over, never mid-download

    def on_done(self, summary):
        self.set_running(False)
        if self.stale:
            self.update_btn.grid(row=0, column=1, sticky="e", padx=(0, 8))
        self.status.configure(text=summary if summary.startswith(("Cancelled", "Failed")) else f"Done · {summary}")
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def on_updated(self, ok):
        if ok:
            os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
        self.updating = False
        self.busy(False)
        self.update_btn.state(["!disabled"])
        if not self.blocked:
            self.go.state(["!disabled"])
        self.status.configure(text="Ready")
        self.show_message("Update failed. Check your internet connection and try again.")
