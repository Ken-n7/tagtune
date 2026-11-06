"""Tk window."""
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .config import CONFIG, OUT_DIR, _job_dirs, log
from .text import validate_url
from .download import preflight, run_job

PALETTES = {
    "light": dict(bg="#F3F0E6", surface="#FAF8F1", text="#1D2A21", muted="#6E7A70", border="#C8C1AC",
                  accent="#1E7A3E", hover="#14592C", on_accent="#FAF8F1", error="#B3261E"),
    "dark": dict(bg="#090D0B", surface="#0E1511", text="#C6F2D2", muted="#5F8A6C", border="#1E3427",
                 accent="#4ADE80", hover="#86EFAC", on_accent="#04120A", error="#FF6B6B"),
}
SYMBOL = {"ok": "[ok]", "keep": "[~~]", "skip": "[==]", "fail": "[!!]"}
HINT = "# downloaded songs will appear here\n"


def system_theme():
    try:
        out = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        return "dark" if "dark" in out else "light"
    except (OSError, subprocess.SubprocessError):
        return "light"


def load_settings():
    try:
        settings = json.loads(CONFIG.read_text())
        return settings if isinstance(settings, dict) else {}
    except (OSError, ValueError):
        return {}


def save_setting(key, value):
    try:
        CONFIG.parent.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(json.dumps({**load_settings(), key: value}))
    except OSError:
        log.exception("could not save %s", key)


def load_theme():
    theme = load_settings().get("theme")
    return theme if theme in PALETTES else system_theme()


def scan_library(folder):
    """Every MP3 under folder, newest first: (path, title, artist, album, seconds, bytes)."""
    from mutagen import MutagenError
    from mutagen.easyid3 import EasyID3
    from mutagen.mp3 import MP3

    songs = []
    for path in Path(folder).rglob("*.mp3"):
        artist, _, title = path.stem.rpartition(" - ")
        tags, seconds = {}, 0
        try:
            audio = MP3(path, ID3=EasyID3)
            tags, seconds = audio.tags or {}, int(audio.info.length)
        except (MutagenError, OSError):
            pass
        get = lambda key, default: (tags.get(key) or [default])[0]  # noqa: E731
        try:
            stat = path.stat()
        except OSError:
            continue
        songs.append((stat.st_mtime, (str(path), get("title", title), get("artist", artist), get("album", ""),
                                       seconds, stat.st_size)))
    return [song for _, song in sorted(songs, key=lambda s: s[0], reverse=True)]


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit in ("B", "KB") else f"{n:.1f} {unit}"
        n /= 1024


def load_out_dir():
    folder = load_settings().get("folder")
    return Path(folder) if isinstance(folder, str) and folder else OUT_DIR


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
        self.out_dir = load_out_dir()
        self.style = ttk.Style(root)
        # Tk's folder picker hardcodes a white list with black text; point both at theme colors.
        root.tk.eval("auto_load ::tk::IconList")
        body = root.tk.eval("lindex [info class definition ::tk::IconList Create] 1")
        body = body.replace("-background white", "-background $::tagtune_bg").replace("set fill black", "set fill $::tagtune_fg")
        root.tk.call("oo::define", "::tk::IconList", "method", "Create", "", body)
        self.style.theme_use("clam")

        mono = tkfont.nametofont("TkFixedFont").actual("family")
        for name in ("TkDefaultFont", "TkTextFont", "TkHeadingFont", "TkMenuFont"):
            tkfont.nametofont(name).configure(family=mono, size=11)
        title_font = tkfont.nametofont("TkDefaultFont").copy()
        title_font.configure(size=17, weight="bold")
        bold = tkfont.nametofont("TkDefaultFont").copy()
        bold.configure(weight="bold")
        self.style.configure("Title.TLabel", font=title_font)
        self.style.configure("Accent.TButton", font=bold)
        self.section_font = tkfont.nametofont("TkDefaultFont").copy()
        self.section_font.configure(size=10, weight="bold")

        root.title("TagTune")
        root.geometry("760x540")
        root.minsize(700, 460)
        self.songs = []
        f = ttk.Frame(root, padding=16)
        root.bind("<Configure>", lambda e: e.widget is root and self.layout(e.width, e.height))
        self.frame = f
        f.columnconfigure(0, weight=1)
        f.rowconfigure(2, weight=1)

        head = ttk.Frame(f)
        head.grid(row=0, column=0, sticky="ew")
        head.columnconfigure(0, weight=1)
        ttk.Label(head, text="$ tagtune", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        tools = ttk.Frame(head)
        tools.grid(row=0, column=1, sticky="e")
        self.update_btn = ttk.Button(tools, text="[ update downloader ]", command=self.update_downloader)
        self.folder_btn = ttk.Button(tools, text="[ change folder ]", command=self.change_folder)
        self.folder_btn.grid(row=0, column=1, padx=(0, 8))
        ttk.Button(tools, text="[ open folder ]", command=self.open_folder).grid(row=0, column=2, padx=(0, 8))
        self.theme_btn = ttk.Button(tools, command=self.toggle_theme, takefocus=False)
        self.theme_btn.grid(row=0, column=3)
        self.stats = ttk.Label(head, style="Muted.TLabel")
        self.stats.grid(row=1, column=0, columnspan=2, sticky="w")

        ttk.Separator(f).grid(row=1, column=0, sticky="ew", pady=12)
        body = ttk.Frame(f)
        body.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1, uniform="side")
        body.columnconfigure(1, weight=0)

        # left: download
        d = ttk.Frame(body)
        d.grid(row=0, column=0, sticky="nsew")
        d.columnconfigure(0, weight=1)
        d.rowconfigure(7, weight=1)
        ttk.Label(d, text="// download", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(d, text="> paste a youtube or youtube music link", style="Muted.TLabel").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(8, 4))
        self.url = tk.StringVar()
        self.entry = ttk.Entry(d, textvariable=self.url)
        self.entry.grid(row=2, column=0, sticky="ew", padx=(0, 8), ipady=4)
        self.go = ttk.Button(d, text="[ download ]", style="Accent.TButton", command=self.start)
        self.go.grid(row=2, column=1, sticky="ew")

        self.exact = tk.BooleanVar(value=False)
        self.exact_box = ttk.Checkbutton(d, variable=self.exact,
                                         text="--exact   skip the search for the official version")
        self.exact_box.grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 0))

        self.msg = ttk.Label(d, style="Error.TLabel", justify="left")
        self.msg.grid(row=4, column=0, columnspan=2, sticky="w", pady=(4, 0))
        self.bar = ttk.Progressbar(d, maximum=100)
        self.bar.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(12, 4))
        self.status = ttk.Label(d, text="ready", style="Muted.TLabel")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w")

        self.list = tk.Text(d, height=8, wrap="word", relief="flat", borderwidth=0, highlightthickness=1,
                            padx=12, pady=8, spacing1=2, spacing3=2, cursor="arrow", takefocus=False,
                            font="TkDefaultFont")
        self.list.grid(row=7, column=0, columnspan=2, sticky="nsew", pady=(12, 0))
        self.list.insert("end", HINT, "muted")
        self.list.configure(state="disabled")
        self.empty = True

        # right: library, only shown when the window is wide enough for two columns
        lib = self.lib = ttk.Frame(body)
        lib.columnconfigure(0, weight=1)
        lib.rowconfigure(1, weight=1)
        ttk.Label(lib, text="// library", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.fill_library())
        ttk.Label(lib, text="filter:", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(0, 8))
        ttk.Entry(lib, textvariable=self.query, width=24).grid(row=0, column=2, columnspan=2, sticky="e")
        cols = ("title", "artist", "album", "time")
        self.tree = ttk.Treeview(lib, columns=cols, show="headings", selectmode="browse")
        for col, width, stretch in (("title", 160, True), ("artist", 110, True), ("album", 110, True),
                                    ("time", 56, False)):
            self.tree.heading(col, text=col, anchor="w")
            self.tree.column(col, width=width, minwidth=50, stretch=stretch, anchor="e" if col == "time" else "w")
        self.tree.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(8, 0))
        sb = ttk.Scrollbar(lib, orient="vertical", command=self.tree.yview)
        sb.grid(row=1, column=3, sticky="ns", pady=(8, 0))
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<Double-1>", lambda e: self.play_selected())
        self.tree.bind("<Return>", lambda e: (self.play_selected(), "break")[1])
        self.body = body

        d.bind("<Configure>", lambda e: self.msg.configure(wraplength=max(200, e.width - 16)))
        root.bind("<Return>", lambda e: self.start())
        root.bind("<Escape>", lambda e: self.stop())
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.apply_theme()

        self.check_folder()
        self.refresh_library()
        try:
            clip, err = validate_url(root.clipboard_get())
        except tk.TclError:
            clip, err = None, "empty clipboard"
        if clip and not err:
            self.url.set(clip)
            self.entry.select_range(0, "end")
        self.entry.focus_set()
        root.after(100, self.poll)

    # layout

    def layout(self, width, height):
        self.frame.place(x=0, y=0, width=width, height=height)
        wide = width >= 900
        if wide and not self.lib.winfo_ismapped():
            self.body.columnconfigure(1, weight=1, uniform="side")
            self.lib.grid(row=0, column=1, sticky="nsew", padx=(24, 0))
        elif not wide and self.lib.winfo_manager():
            self.lib.grid_remove()
            self.body.columnconfigure(1, weight=0, uniform="")

    # library

    def refresh_library(self):
        folder = self.out_dir
        threading.Thread(target=lambda: self.q.put(("library", folder, scan_library(folder))), daemon=True).start()

    def on_library(self, folder, songs):
        if folder != self.out_dir:
            return  # the folder changed while this scan ran; a newer one is on its way
        self.songs = songs
        playlists = {Path(s[0]).parent for s in songs} - {folder}
        size = sum(s[5] for s in songs)
        count = f"{len(songs)} song{'s' * (len(songs) != 1)}"
        lists = f"  │  {len(playlists)} playlist{'s' * (len(playlists) != 1)}" if playlists else ""
        self.stats.configure(text=f"{count}{lists}  │  {human_size(size)}  │  {folder}")
        self.fill_library()

    def fill_library(self):
        q = self.query.get().casefold().strip()
        self.tree.delete(*self.tree.get_children())
        for path, title, artist, album, seconds, _ in self.songs:
            if q and q not in f"{title} {artist} {album}".casefold():
                continue
            self.tree.insert("", "end", iid=path, values=(title, artist, album, f"{seconds // 60}:{seconds % 60:02d}"))

    def play_selected(self):
        sel = self.tree.selection()
        if sel:
            try:
                subprocess.Popen(["xdg-open", sel[0]])
            except OSError:
                self.show_message("Couldn't open that song.")

    # theme

    def apply_theme(self):
        p, s = PALETTES[self.theme], self.style
        self.root.configure(bg=p["bg"])
        s.configure(".", background=p["bg"], foreground=p["text"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], troughcolor=p["border"], focuscolor=p["accent"])
        s.configure("Muted.TLabel", foreground=p["muted"])
        s.configure("Section.TLabel", foreground=p["accent"], font=self.section_font)
        s.configure("Title.TLabel", foreground=p["text"])
        s.configure("Treeview", background=p["surface"], fieldbackground=p["surface"], foreground=p["text"],
                    bordercolor=p["border"], lightcolor=p["surface"], darkcolor=p["surface"], rowheight=28)
        s.map("Treeview", background=[("selected", p["accent"])], foreground=[("selected", p["on_accent"])])
        s.configure("Treeview.Heading", background=p["surface"], foreground=p["accent"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], padding=(8, 4))
        s.map("Treeview.Heading", background=[("active", p["surface"])])
        s.configure("TSeparator", background=p["border"])
        s.configure("Error.TLabel", foreground=p["error"])
        s.configure("TEntry", fieldbackground=p["surface"], foreground=p["text"], insertcolor=p["text"],
                    lightcolor=p["surface"], darkcolor=p["surface"], padding=6,
                    selectbackground=p["accent"], selectforeground=p["on_accent"])
        s.map("TEntry", bordercolor=[("disabled", p["border"]), ("focus", p["accent"])],
              lightcolor=[("disabled", p["bg"]), ("focus", p["accent"])],
              darkcolor=[("disabled", p["bg"]), ("focus", p["accent"])], fieldbackground=[("disabled", p["bg"])],
              foreground=[("disabled", p["muted"])])
        s.configure("TButton", background=p["bg"], foreground=p["muted"], bordercolor=p["border"],
                    lightcolor=p["bg"], darkcolor=p["bg"], padding=(10, 5))
        s.map("TButton", background=[("active", p["bg"])], foreground=[("disabled", p["border"]), ("active", p["text"])],
              bordercolor=[("active", p["muted"]), ("focus", p["accent"])],
              lightcolor=[("active", p["bg"])], darkcolor=[("active", p["bg"])])
        s.configure("Accent.TButton", background=p["bg"], foreground=p["accent"], bordercolor=p["accent"],
                    lightcolor=p["bg"], darkcolor=p["bg"], focuscolor=p["accent"], padding=(14, 5))
        s.map("Accent.TButton", background=[("disabled", p["bg"]), ("active", p["accent"])],
              foreground=[("disabled", p["border"]), ("active", p["on_accent"])],
              bordercolor=[("disabled", p["border"])],
              lightcolor=[("disabled", p["bg"]), ("active", p["accent"])],
              darkcolor=[("disabled", p["bg"]), ("active", p["accent"])])
        s.configure("TCheckbutton", background=p["bg"], foreground=p["text"], indicatorbackground=p["surface"],
                    indicatorforeground=p["on_accent"], upperbordercolor=p["muted"], lowerbordercolor=p["muted"],
                    padding=(0, 2))
        s.map("TCheckbutton", background=[("active", p["bg"])], foreground=[("disabled", p["muted"])],
              indicatorbackground=[("selected", p["accent"]), ("active", p["surface"])],
              upperbordercolor=[("selected", p["accent"]), ("focus", p["accent"])],
              lowerbordercolor=[("selected", p["accent"]), ("focus", p["accent"])])
        s.configure("TScrollbar", background=p["border"], troughcolor=p["surface"], bordercolor=p["surface"],
                    arrowcolor=p["muted"], lightcolor=p["border"], darkcolor=p["border"])
        s.map("TScrollbar", background=[("active", p["muted"])])
        s.configure("TMenubutton", background=p["surface"], foreground=p["text"], arrowcolor=p["muted"])
        self.root.setvar("::tagtune_bg", p["surface"])
        self.root.setvar("::tagtune_fg", p["text"])
        s.configure("Horizontal.TProgressbar", background=p["accent"], lightcolor=p["accent"],
                    darkcolor=p["accent"], bordercolor=p["border"], troughcolor=p["surface"], thickness=10)
        self.list.configure(background=p["surface"], foreground=p["text"], highlightbackground=p["border"],
                            highlightcolor=p["border"], selectbackground=p["accent"],
                            selectforeground=p["on_accent"], insertbackground=p["surface"])
        for tag, color in (("ok", "accent"), ("keep", "text"), ("skip", "muted"), ("fail", "error"), ("muted", "muted")):
            self.list.tag_configure(tag, foreground=p[color])
        self.theme_btn.configure(text="[ light ]" if self.theme == "dark" else "[ dark ]")

    def toggle_theme(self):
        self.theme = "light" if self.theme == "dark" else "dark"
        self.apply_theme()
        save_setting("theme", self.theme)

    # actions

    def check_folder(self):
        fatal, warnings = preflight(self.out_dir)
        self.blocked = bool(fatal)
        self.go.state(["disabled" if fatal else "!disabled"])
        self.show_message(fatal or " ".join(warnings))
        self.status.configure(text=f"saving to {self.out_dir}")

    def change_folder(self):
        from tkinter import filedialog
        self.root.tk.eval("destroy .__tk_choosedir")  # rebuilt each time so it picks up the current theme

        folder = filedialog.askdirectory(title="Save songs to", initialdir=self.out_dir, mustexist=False)
        if not folder:
            return
        self.out_dir = Path(folder)
        save_setting("folder", folder)
        self.check_folder()
        self.refresh_library()

    def show_message(self, text):
        self.msg.configure(text=f"! {text}" if text else "")

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
        threading.Thread(target=run_job, args=(url, self.out_dir, emit, self.cancel, self.confirm, self.exact.get()),
                         daemon=True).start()

    def stop(self):
        if self.running:
            self.cancel.set()
            self.status.configure(text="Cancelling…")

    def set_running(self, running):
        self.running = running
        self.entry.state(["disabled" if running else "!disabled"])
        self.exact_box.state(["disabled" if running else "!disabled"])
        self.folder_btn.state(["disabled" if running else "!disabled"])
        self.go.configure(text="[ cancel ]" if running else "[ download ]",
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
            self.out_dir.mkdir(parents=True, exist_ok=True)
            subprocess.Popen(["xdg-open", str(self.out_dir)])
        except OSError:
            self.show_message(f"Couldn't open {self.out_dir}.")

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
        self.list.insert("end", f"{SYMBOL[kind]} ", kind, name, "", f"   {note}\n", "fail" if kind == "fail" else "muted")
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
        self.refresh_library()
        if self.stale:
            self.update_btn.grid(row=0, column=0, padx=(0, 8))
        self.status.configure(text=summary if summary.startswith(("Cancelled", "Failed")) else f"done · {summary}")
        self.entry.focus_set()
        self.entry.select_range(0, "end")

    def on_updated(self, ok):
        if ok:
            os.execv(sys.executable, [sys.executable, "-m", "tagtune"])
        self.updating = False
        self.busy(False)
        self.update_btn.state(["!disabled"])
        if not self.blocked:
            self.go.state(["!disabled"])
        self.status.configure(text="ready")
        self.show_message("Update failed. Check your internet connection and try again.")
