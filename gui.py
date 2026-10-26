"""Tkinter front end for downloader.py."""
import csv
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import asdict, fields
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk

import downloader as dl
from downloader import (AUDIO_QUALITIES, COOKIE_BROWSERS, VIDEO_QUALITIES, AskRename, Downloader,
                        FileEnded, FileProcessing, FileProgress, FileStarted, Finished, ItemDone, Log,
                        Options, Phase, Started)

SETTINGS_FILE = Path(__file__).with_name('settings.json')  # Remembers the options between runs
POLL_MS = 100
MAX_WORKERS = 8
RENAME_EXAMPLES = 5     # Files listed when asking whether to rename
SEP = '  ·  '


def fmt_bytes(n):
    for unit in ('B', 'KiB', 'MiB', 'GiB'):
        if n < 1024 or unit == 'GiB':
            return f"{n:.0f} {unit}" if unit == 'B' else f"{n:.1f} {unit}"
        n /= 1024


def fmt_time(seconds):
    if seconds is None:
        return '--:--'
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def quality_labels(fmt):
    if fmt == 'mp3':
        return [f"{q} kbps" for q in AUDIO_QUALITIES]
    return ["Best available" if q == 'best' else f"Up to {q}p" for q in VIDEO_QUALITIES]


def load_settings():
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return Options()
    known = {f.name for f in fields(Options)}
    try:
        return Options(**{k: v for k, v in saved.items() if k in known})
    except TypeError:
        return Options()


def save_settings(options):
    try:
        SETTINGS_FILE.write_text(json.dumps(asdict(options), indent=2), encoding='utf-8')
    except OSError:
        pass  # Not worth bothering the user about


class ActiveFile:
    """A download in progress. Video downloads come in two parts (video, then audio), so the bytes
    of finished parts are kept in `done_parts` and the progress covers both."""

    def __init__(self, title):
        self.title = title
        self.done_parts = 0
        self.downloaded = 0
        self.total = None
        self.speed = None
        self.eta = None
        self.processing = False

    def update(self, event):
        self.downloaded = self.done_parts + event.downloaded
        self.total = event.total and self.done_parts + event.total
        self.speed, self.eta = event.speed, event.eta
        self.processing = False

    def part_finished(self):
        self.done_parts = self.downloaded
        self.processing = True


class Stats:
    """Overall progress, built from the downloader's events."""

    def __init__(self):
        self.total = 0
        self.to_download = 0
        self.counts = dict.fromkeys(('ok', 'skipped', 'failed', 'pending'), 0)
        self.started = None
        self.ended = None
        self.bytes_done = 0     # Bytes of downloads that have ended
        self.active = {}        # worker key -> ActiveFile, oldest first
        self.running = False

    @property
    def done(self):
        return sum(self.counts.values())

    def elapsed(self):
        if not self.started:
            return 0
        return (self.ended or time.monotonic()) - self.started

    def speed(self):
        return sum(a.speed or 0 for a in self.active.values() if not a.processing)

    def avg_speed(self):
        elapsed = self.elapsed()
        return (self.bytes_done + sum(a.downloaded for a in self.active.values())) / elapsed if elapsed else 0

    def eta(self):
        # Average time per attempted download so far; skipped files take no time so they're left out
        attempted = self.counts['ok'] + self.counts['failed']
        remaining = max(self.to_download - attempted - self.counts['pending'], 0)
        if not remaining:
            return 0
        return self.elapsed() / attempted * remaining if attempted else None


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Downloader")
        self.geometry("920x800")
        self.minsize(700, 600)

        self.events = queue.Queue()
        self.downloader = None
        self.worker = None
        self.csv_items = []     # (url, folder) pairs from the loaded CSV
        self.stats = Stats()
        self.inputs = []        # Widgets disabled while a download runs

        opts = load_settings()
        self.format_var = tk.StringVar(value=opts.format)
        self.quality_var = tk.StringVar()
        self.naming_var = tk.StringVar(value=opts.naming)
        self.quality_name_var = tk.BooleanVar(value=opts.quality_in_name)
        self.metadata_var = tk.BooleanVar(value=opts.metadata)
        self.cookies_var = tk.StringVar(value=opts.cookies if opts.cookies in COOKIE_BROWSERS else 'none')
        self.workers_var = tk.IntVar(value=min(max(opts.workers, 1), MAX_WORKERS))
        # A relative folder is relative to this script, not to wherever it was launched from
        self.output_var = tk.StringVar(value=os.path.normpath(SETTINGS_FILE.parent / opts.output))

        self.build_urls()
        self.build_options(opts)
        self.build_controls()
        self.build_progress()
        self.build_log()
        self.reset_progress()

        self.bind('<Control-Return>', lambda e: self.start())
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(POLL_MS, self.poll)

        missing = dl.missing_tools()
        if missing:
            self.after(200, lambda: messagebox.showwarning(
                "ffmpeg not found", f"{' and '.join(missing)} not found on PATH. Downloads will fail "
                "until ffmpeg is installed (e.g. 'winget install ffmpeg')."))

    # ---- layout ----

    def build_urls(self):
        frame = ttk.LabelFrame(self, text="URLs: videos, music or playlists, one per line")
        frame.pack(fill='x', padx=8, pady=4)
        self.urls_text = scrolledtext.ScrolledText(frame, height=6, wrap='none', undo=True)
        self.urls_text.pack(fill='both', expand=True, padx=6, pady=6)

        row = ttk.Frame(frame)
        row.pack(fill='x', padx=6, pady=(0, 6))
        self.inputs += [
            ttk.Button(row, text="Paste", command=self.paste_urls),
            ttk.Button(row, text="Load CSV...", command=self.load_csv),
            ttk.Button(row, text="Clear", command=self.clear_inputs),
        ]
        for i, button in enumerate(self.inputs):
            button.pack(side='left', padx=(0 if i == 0 else 6, 0))
        self.csv_label = ttk.Label(row, text="No CSV loaded")
        self.csv_label.pack(side='left', padx=12)
        self.inputs.append(self.urls_text)

    def build_options(self, opts):
        frame = ttk.LabelFrame(self, text="Options")
        frame.pack(fill='x', padx=8, pady=4)
        frame.columnconfigure(1, weight=1)

        def row(r, label):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky='w', padx=6, pady=3)
            holder = ttk.Frame(frame)
            holder.grid(row=r, column=1, sticky='we', padx=(0, 6), pady=3)
            return holder

        def add(widget, **pack):
            widget.pack(side='left', **pack)
            self.inputs.append(widget)
            return widget

        r = row(0, "Download as:")
        add(ttk.Radiobutton(r, text="MP3 (audio)", value='mp3', variable=self.format_var,
                            command=self.update_qualities))
        add(ttk.Radiobutton(r, text="MP4 (video)", value='mp4', variable=self.format_var,
                            command=self.update_qualities), padx=12)

        r = row(1, "Quality:")
        self.quality_box = add(ttk.Combobox(r, textvariable=self.quality_var, state='readonly', width=22))
        self.quality_box.bind('<<ComboboxSelected>>', lambda e: self.update_example())
        self.update_qualities(opts.quality)

        r = row(2, "File naming:")
        add(ttk.Radiobutton(r, text="YT Title", value='title', variable=self.naming_var,
                            command=self.update_example))
        add(ttk.Radiobutton(r, text="YT Title - Channel/Author", value='title_channel',
                            variable=self.naming_var, command=self.update_example), padx=12)
        add(ttk.Checkbutton(r, text="Add quality", variable=self.quality_name_var,
                            command=self.update_example), padx=(0, 12))
        self.example = ttk.Label(r, foreground='gray')
        self.example.pack(side='left')
        self.update_example()

        r = row(3, "Tags:")
        add(ttk.Checkbutton(r, text="Write title/artist metadata into the files", variable=self.metadata_var))

        r = row(4, "Cookies from:")
        add(ttk.Combobox(r, textvariable=self.cookies_var, values=COOKIE_BROWSERS, state='readonly', width=22))
        ttk.Label(r, text="browser you're logged into YouTube with", foreground='gray').pack(side='left', padx=8)

        r = row(5, "Parallel downloads:")
        add(ttk.Spinbox(r, from_=1, to=MAX_WORKERS, textvariable=self.workers_var, width=5, state='readonly'))
        ttk.Label(r, text="more than 2-3 often gets you rate-limited by YouTube",
                  foreground='gray').pack(side='left', padx=8)

        r = row(6, "Save to:")
        add(ttk.Entry(r, textvariable=self.output_var), fill='x', expand=True)
        add(ttk.Button(r, text="Browse...", command=self.browse_output), padx=(6, 0))

    def build_controls(self):
        frame = ttk.Frame(self)
        frame.pack(fill='x', padx=8, pady=4)
        self.start_btn = ttk.Button(frame, text="Start download  (Ctrl+Enter)", command=self.start)
        self.start_btn.pack(side='left')
        self.stop_btn = ttk.Button(frame, text="Stop", command=self.stop, state='disabled')
        self.stop_btn.pack(side='left', padx=6)
        ttk.Button(frame, text="Open folder", command=self.open_output).pack(side='left')

    def build_progress(self):
        frame = ttk.LabelFrame(self, text="Progress")
        frame.pack(fill='x', padx=8, pady=4)
        frame.columnconfigure(1, weight=1)

        self.status = ttk.Label(frame)
        self.status.grid(row=0, column=0, columnspan=3, sticky='w', padx=6, pady=(4, 2))

        def bar(r, label):
            ttk.Label(frame, text=label).grid(row=r, column=0, sticky='w', padx=6, pady=(6, 0))
            progress = ttk.Progressbar(frame, maximum=1)
            progress.grid(row=r, column=1, sticky='we', padx=6, pady=(6, 2))
            pct = ttk.Label(frame, width=6, anchor='e')
            pct.grid(row=r, column=2, padx=6, pady=(6, 0))
            info = ttk.Label(frame, foreground='gray')
            info.grid(row=r + 1, column=1, columnspan=2, sticky='w', padx=6, pady=(0, 4))
            return progress, pct, info

        self.file_bar, self.file_pct, self.file_info = bar(1, "File:")
        self.overall_bar, self.overall_pct, self.overall_info = bar(3, "Overall:")

    def build_log(self):
        frame = ttk.LabelFrame(self, text="Log")
        frame.pack(fill='both', expand=True, padx=8, pady=4)
        self.log_text = scrolledtext.ScrolledText(frame, height=8, state='disabled', wrap='word')
        self.log_text.pack(fill='both', expand=True, padx=6, pady=6)

    # ---- option helpers ----

    def update_qualities(self, selected=None):
        fmt = self.format_var.get()
        values = AUDIO_QUALITIES if fmt == 'mp3' else VIDEO_QUALITIES
        self.quality_box['values'] = quality_labels(fmt)
        self.quality_box.current(values.index(selected) if selected in values else 0)
        if hasattr(self, 'example'):
            self.update_example()

    def selected_quality(self):
        values = AUDIO_QUALITIES if self.format_var.get() == 'mp3' else VIDEO_QUALITIES
        return values[self.quality_box.current()]

    def update_example(self):
        name = "Song Title" + (" - Channel" if self.naming_var.get() == 'title_channel' else '')
        if self.quality_name_var.get():
            quality = self.selected_quality()
            if self.format_var.get() == 'mp3':
                name += f" [{quality}kbps]"
            else:
                name += f" [{'1080' if quality == 'best' else quality}p]"  # Real resolution is used
        self.example.config(text=f"e.g. {name}.{self.format_var.get()}")

    def current_options(self):
        return Options(format=self.format_var.get(), quality=self.selected_quality(),
                       naming=self.naming_var.get(), quality_in_name=self.quality_name_var.get(),
                       metadata=self.metadata_var.get(), cookies=self.cookies_var.get(),
                       workers=self.workers_var.get(), output=self.output_var.get().strip() or 'downloads')

    def set_running(self, running):
        state = 'disabled' if running else 'normal'
        for widget in self.inputs:
            if isinstance(widget, (ttk.Combobox, ttk.Spinbox)):
                widget.config(state='disabled' if running else 'readonly')
            else:
                widget.config(state=state)
        self.start_btn.config(state=state)
        self.stop_btn.config(state='normal' if running else 'disabled')

    # ---- actions ----

    def paste_urls(self):
        try:
            text = self.clipboard_get()
        except tk.TclError:
            return
        current = self.urls_text.get('1.0', 'end-1c')
        if current and not current.endswith('\n'):
            text = '\n' + text
        self.urls_text.insert('end', text.strip() + '\n')

    def load_csv(self):
        path = filedialog.askopenfilename(filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            self.csv_items = dl.load_csv(path)
        except (OSError, UnicodeDecodeError, csv.Error) as err:
            messagebox.showerror("Couldn't read CSV", str(err))
            return
        self.csv_label.config(text=f"{os.path.basename(path)}: {len(self.csv_items)} URL(s)")

    def clear_inputs(self):
        self.urls_text.delete('1.0', 'end')
        self.csv_items = []
        self.csv_label.config(text="No CSV loaded")

    def browse_output(self):
        path = filedialog.askdirectory(initialdir=self.output_var.get())
        if path:
            self.output_var.set(os.path.normpath(path))

    def open_output(self):
        path = self.output_var.get()
        os.makedirs(path, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(path)
        else:
            subprocess.Popen(['open' if sys.platform == 'darwin' else 'xdg-open', path])

    def collect_items(self):
        lines = self.urls_text.get('1.0', 'end').splitlines()
        typed = [(line.strip(), None) for line in lines if line.strip() and not line.lstrip().startswith('#')]
        seen, items = set(), []
        for url, folder in typed + self.csv_items:
            if (url, folder) not in seen:
                seen.add((url, folder))
                items.append((url, folder))
        return items

    def start(self):
        if self.worker and self.worker.is_alive():
            return
        items = self.collect_items()
        if not items:
            messagebox.showwarning("Nothing to download", "Enter at least one URL or load a CSV file.")
            return
        options = self.current_options()
        save_settings(options)

        self.reset_progress()
        self.status.config(text="Starting...")
        self.set_running(True)
        self.downloader = Downloader(options, self.events.put)
        self.worker = threading.Thread(target=self.downloader.run, args=(items,), daemon=True)
        self.worker.start()

    def stop(self):
        # Running downloads are cancelled; their partial files are resumed next time
        self.downloader.stop()
        self.stop_btn.config(state='disabled')
        self.status.config(text="Stopping...")

    def on_close(self):
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("Quit", "A download is running. Quit anyway?\n"
                                       "Unfinished files are resumed next time."):
                return
            self.downloader.stop()
        save_settings(self.current_options())
        self.destroy()
        if self.worker and self.worker.is_alive():
            os._exit(0)  # Don't wait for a conversion or playlist fetch that can't be interrupted

    # ---- progress ----

    def reset_progress(self):
        self.stats = Stats()
        for progress in (self.file_bar, self.overall_bar):
            progress.config(maximum=1, value=0)
        for label in (self.file_pct, self.overall_pct):
            label.config(text="0%")
        for label in (self.file_info, self.overall_info):
            label.config(text="")
        self.status.config(text="Idle")

    def handle(self, event):
        stats = self.stats
        match event:
            case Phase(text):
                self.status.config(text=text)
            case Started(total, to_download):
                stats.total, stats.to_download = total, to_download
                stats.started = time.monotonic()
                stats.running = True
            case FileStarted(key, title):
                stats.active[key] = ActiveFile(title)
            case FileProgress(key=key) if key in stats.active:
                stats.active[key].update(event)
            case FileProcessing(key) if key in stats.active:
                stats.active[key].part_finished()
            case FileEnded(key):
                ended = stats.active.pop(key, None)
                if ended:
                    stats.bytes_done += ended.downloaded
            case AskRename(renames, reply):
                reply.put(self.ask_rename(renames))
            case ItemDone(status):
                stats.counts[status] += 1
            case Finished():
                stats.running = False
                stats.ended = time.monotonic()
                stats.active.clear()
                self.set_running(False)
                self.status.config(text="Finished")
                self.file_info.config(text="")

    def ask_rename(self, renames):
        examples = '\n'.join(f"{os.path.basename(old)}  ->  {os.path.basename(new)}"
                             for old, new in renames[:RENAME_EXAMPLES])
        if len(renames) > RENAME_EXAMPLES:
            examples += f"\n...and {len(renames) - RENAME_EXAMPLES} more"
        return messagebox.askyesno(
            "Rename existing files?",
            f"{len(renames)} file(s) were already downloaded under the other naming style:\n\n{examples}\n\n"
            "Yes: rename them to the new naming instead of downloading them again.\n"
            "No: download them again under the new name (the old files are kept).")

    def poll(self):
        lines = []
        try:
            while True:
                event = self.events.get_nowait()
                if isinstance(event, Log):
                    lines.append(event.message)
                    continue
                if isinstance(event, AskRename) and lines:  # Show the log up to the question first
                    self.append_log('\n'.join(lines))
                    lines = []
                self.handle(event)
        except queue.Empty:
            pass
        if lines:
            self.append_log('\n'.join(lines))
        if self.stats.running:
            self.refresh_file()
        self.refresh_overall()
        self.after(POLL_MS, self.poll)

    def append_log(self, text):
        at_bottom = self.log_text.yview()[1] >= 0.999
        self.log_text.config(state='normal')
        self.log_text.insert('end', text + '\n')
        self.log_text.config(state='disabled')
        if at_bottom:  # Don't yank the view away while the user scrolls back
            self.log_text.see('end')

    def refresh_file(self):
        # The file bar follows the oldest running download; the others are counted in the status line
        if not self.stats.active:
            return
        active = next(iter(self.stats.active.values()))
        extra = len(self.stats.active) - 1
        self.status.config(text=f"Downloading: {active.title}" + (f"  (+{extra} more in parallel)" if extra else ''))
        if active.processing:
            self.file_bar.config(maximum=1, value=1)
            self.file_pct.config(text="100%")
            self.file_info.config(text="Converting / merging...")
            return
        fraction = active.downloaded / active.total if active.total else 0
        self.file_bar.config(maximum=1, value=fraction)
        self.file_pct.config(text=f"{fraction:.0%}")
        size = fmt_bytes(active.downloaded) + (f" of {fmt_bytes(active.total)}" if active.total else '')
        speed = f"{fmt_bytes(active.speed)}/s" if active.speed else '-- /s'
        self.file_info.config(text=SEP.join((size, speed, f"ETA {fmt_time(active.eta)}")))

    def refresh_overall(self):
        stats = self.stats
        if not stats.total:
            return
        self.overall_bar.config(maximum=stats.total, value=stats.done)
        self.overall_pct.config(text=f"{stats.done / stats.total:.0%}")
        c = stats.counts
        counts = SEP.join((f"Files {stats.done}/{stats.total}", f"downloaded {c['ok']}",
                           f"already had {c['skipped']}", f"failed {c['failed']}"))
        timing = SEP.join((f"Speed {fmt_bytes(stats.speed())}/s (avg {fmt_bytes(stats.avg_speed())}/s)",
                           f"elapsed {fmt_time(stats.elapsed())}", f"ETA {fmt_time(stats.eta())}"))
        self.overall_info.config(text=counts + '\n' + timing)


def main():
    if sys.platform == 'win32':
        try:  # Crisp text on high-DPI screens
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except (AttributeError, OSError):
            pass
    App().mainloop()


if __name__ == "__main__":
    main()
