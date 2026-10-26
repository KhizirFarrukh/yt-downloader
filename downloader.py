"""Download engine: turns video/playlist URLs into MP3 or MP4 files with yt-dlp.

Has no UI code. Progress is reported by calling `emit` with the event objects below, from worker
threads, so a UI should hand them over to its own thread (e.g. through a queue).
"""
import csv
import glob
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

import yt_dlp
from yt_dlp.cookies import extract_cookies_from_browser
from yt_dlp.utils import DownloadCancelled, DownloadError, remove_terminal_sequences, sanitize_filename

DURATION_TOLERANCE = 3    # Seconds a fallback match may differ from the original track's length
SEARCH_CANDIDATES = 5     # How many search results to check when looking for a fallback
FETCH_WORKERS = 4         # URLs resolved in parallel (listing a playlist is cheap compared to downloading)
PROGRESS_INTERVAL = 0.25  # Seconds between progress events per download

AUDIO_QUALITIES = ['320', '256', '192', '128']                         # kbps
VIDEO_QUALITIES = ['best', '2160', '1440', '1080', '720', '480', '360']  # max height
COOKIE_BROWSERS = ['firefox', 'chrome', 'edge', 'brave', 'opera', 'vivaldi', 'none']
UNAVAILABLE_TITLES = ('[Private video]', '[Deleted video]')

# Downloaded/converted-from files; next to a finished file they mean it was interrupted
SOURCE_EXTS = ('.webm', '.m4a', '.opus', '.mp4', '.mkv')
TEMP_SUFFIXES = ('.part', '.ytdl')


@dataclass
class Options:
    format: str = 'mp3'         # 'mp3' or 'mp4'
    quality: str = '320'        # kbps for mp3, max height (or 'best') for mp4
    naming: str = 'title'       # 'title' or 'title_channel'
    cookies: str = 'firefox'    # Browser to borrow the YouTube login from, or 'none'
    output: str = 'downloads'
    workers: int = 1            # Parallel downloads. Going much higher tends to get you rate-limited
    quality_in_name: bool = False  # "Title [320kbps].mp3" / "Title [1080p].mp4"
    metadata: bool = True       # Write title/artist/etc. tags into the file

    @property
    def ext(self):
        return '.' + self.format


# Events passed to `emit`. `key` identifies the worker thread, so parallel downloads can be told apart.
@dataclass
class Log:
    message: str


@dataclass
class Phase:
    text: str


@dataclass
class Started:
    total: int          # All items, including ones already downloaded
    to_download: int


@dataclass
class FileStarted:
    key: str
    title: str


@dataclass
class FileProgress:
    key: str
    downloaded: int
    total: int | None
    speed: float | None
    eta: float | None


@dataclass
class FileProcessing:
    key: str            # A part finished downloading; ffmpeg converts/merges next


@dataclass
class FileEnded:
    key: str


@dataclass
class AskRename:
    renames: list       # (old path, new path) of files downloaded under the other naming style
    reply: queue.Queue  # Gets True to rename them, False to download them again under the new name


@dataclass
class ItemDone:
    status: str         # 'ok', 'skipped', 'failed' or 'pending' (left for the next run)


@dataclass
class Finished:
    pass


@dataclass
class Job:
    entry: dict
    stem: str           # Output path without extension
    output_dir: str
    other_stem: str     # Where the file would be under the other naming style


class SilentLogger:
    # Errors are reported through events instead, so yt-dlp's own messages are dropped
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def is_rate_limit(err):
    msg = str(err)
    return '429' in msg or 'Too Many Requests' in msg or 'not a bot' in msg


def clean_error(err):
    return remove_terminal_sequences(str(err)).removeprefix('ERROR: ').strip()


def missing_tools():
    return [tool for tool in ('ffmpeg', 'ffprobe') if not shutil.which(tool)]


def file_duration(path):
    try:
        out = subprocess.run(['ffprobe', '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path],
                             capture_output=True, text=True, timeout=30).stdout
        return float(out.strip())
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def entry_artist(entry):
    return (entry.get('channel') or entry.get('uploader') or '').removesuffix(' - Topic')


def load_csv(path):
    """Returns (url, folder) pairs. The CSV either has a header with a "url" column (and optionally
    "folder", the subfolder of the output folder to save into), or just URLs in the first column.
    Blank rows and rows starting with # are ignored."""
    with open(path, newline='', encoding='utf-8-sig') as f:
        rows = [r for r in csv.reader(f) if r and r[0].strip() and not r[0].strip().startswith('#')]
    if not rows:
        return []
    header = [c.strip().lower() for c in rows[0]]
    if 'url' not in header:
        return [(r[0].strip(), None) for r in rows]
    u = header.index('url')
    fo = header.index('folder') if 'folder' in header else None
    return [(r[u].strip(), (r[fo].strip() or None) if fo is not None and fo < len(r) else None)
            for r in rows[1:] if u < len(r) and r[u].strip()]


class Downloader:
    def __init__(self, options, emit):
        self.opts = options
        self.emit = emit
        self.stop_requested = threading.Event()
        self.rate_limited = threading.Event()  # Once YouTube blocks us, the remaining items are left for later
        self._local = threading.local()        # Per-thread time of the last progress event

    def stop(self):
        """Cancels the running downloads; their partial files are resumed next time."""
        self.stop_requested.set()

    def halted(self):
        return self.stop_requested.is_set() or self.rate_limited.is_set()

    def log(self, message):
        self.emit(Log(message))

    # ---- yt-dlp options ----

    def base_opts(self):
        return {
            'logger': SilentLogger(),
            'quiet': True,
            'no_warnings': True,
            'noprogress': True,
            # Lets yt-dlp solve YouTube's JS challenges; logged-in requests fail without it
            **({'js_runtimes': {'node': {'path': None}}, 'remote_components': ['ejs:github']}
               if shutil.which('node') else {}),
            'retries': 10,
            'fragment_retries': 10,
            'continuedl': True,  # Resume half-finished .part files
            'cookiesfrombrowser': None if self.opts.cookies == 'none' else (self.opts.cookies,),
        }

    def format_opts(self):
        postprocessors = []
        if self.opts.format == 'mp3':
            opts = {'format': 'bestaudio/best'}
            postprocessors.append({'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3',
                                   'preferredquality': self.opts.quality})
        else:
            res = 'res' if self.opts.quality == 'best' else f'res:{self.opts.quality}'
            opts = {
                'format': 'bv*+ba/b',
                # Highest resolution up to the limit, then H.264 + AAC where available so the .mp4
                # plays everywhere (YouTube only has H.264 up to 1080p; above that VP9/AV1 is used)
                'format_sort': [res, 'vcodec:h264', 'acodec:aac'],
                'merge_output_format': 'mp4',
            }
        if self.opts.metadata:
            postprocessors.append({'key': 'FFmpegMetadata', 'add_metadata': True})
        return {**opts, 'postprocessors': postprocessors}

    # ---- naming / files on disk ----

    def partial_exts(self):
        sources = tuple(e for e in SOURCE_EXTS if e != self.opts.ext)  # The finished file isn't a leftover
        return sources + tuple(e + s for e in SOURCE_EXTS for s in TEMP_SUFFIXES)

    def video_tagged(self):
        # A video's resolution is only known once it's downloaded, so MP4s get their tag afterwards
        return self.opts.quality_in_name and self.opts.format == 'mp4'

    def name_tag(self):
        """Quality tag that is part of the stem up front (MP3s, whose bitrate is what we encode at)."""
        if self.opts.quality_in_name and self.opts.format == 'mp3':
            return f" [{self.opts.quality}kbps]"
        return ''

    def finished_file(self, stem):
        """Path of the finished file for this stem, if there is one."""
        if self.video_tagged():
            # Any resolution up to the chosen limit counts: a lower one means that's all the video has.
            # To upgrade, delete the old files.
            limit = None if self.opts.quality == 'best' else int(self.opts.quality)
            for path in glob.glob(glob.escape(stem) + ' [[]*p].mp4'):
                m = re.fullmatch(r' \[(\d+)p\]\.mp4', path[len(stem):])
                if m and (limit is None or int(m[1]) <= limit):
                    return path
            return None
        return stem + self.opts.ext if os.path.exists(stem + self.opts.ext) else None

    def is_complete(self, stem):
        # A finished file next to a leftover source file means ffmpeg was interrupted mid-conversion
        return self.finished_file(stem) is not None and not self.has_partials(stem)

    def has_partials(self, stem):
        return any(os.path.exists(stem + ext) for ext in self.partial_exts())

    def remove_partials(self, stem):
        for ext in self.partial_exts():
            try:
                os.remove(stem + ext)
            except FileNotFoundError:
                pass

    def base_name(self, entry, naming=None):
        """File name (before sanitising) for a naming style (the chosen one by default):
        "Title" or "Title - Channel"."""
        title = entry.get('title') or entry['id']
        artist = entry_artist(entry)
        if (naming or self.opts.naming) == 'title_channel' and artist and artist.lower() != title.lower():
            return f"{title} - {artist}"
        return title

    def assign_stems(self, entries, output_dir, naming=None):
        """Gives every entry of one folder its own output path (without extension), following the
        naming style so already-downloaded files are recognised. Different videos sharing a name
        (covers, remixes, re-uploads) get the artist, or failing that the video ID, appended."""
        naming = naming or self.opts.naming
        ext = self.opts.ext
        outtmpl = os.path.join(output_dir, '%(title)s.%(ext)s')
        with yt_dlp.YoutubeDL({**self.base_opts(), 'outtmpl': outtmpl}) as ydl:
            def stem_for(entry, name):
                info = {'id': entry['id'], 'title': name + self.name_tag(), 'ext': ext[1:]}
                return os.path.splitext(ydl.prepare_filename(info))[0]

            groups = defaultdict(list)
            for entry in entries:
                groups[stem_for(entry, self.base_name(entry, naming)).lower()].append(entry)  # Windows ignores case

            stems = {}
            used = set(groups)
            for group in groups.values():
                plain = stem_for(group[0], self.base_name(group[0], naming))
                owner = group[0]
                if len(group) > 1 and os.path.exists(plain + ext):
                    # Earlier runs saved only one of these under the plain name; its length tells us which one
                    have = file_duration(plain + ext)
                    timed = [e for e in group if e.get('duration') and have]
                    best = min(timed, key=lambda e: abs(e['duration'] - have), default=None)
                    if best and abs(best['duration'] - have) <= DURATION_TOLERANCE + 2:
                        owner = best
                for entry in group:
                    if entry is owner:
                        stems[entry['id']] = plain
                        continue
                    artist = entry_artist(entry)
                    if naming == 'title_channel' or artist.lower() == (entry.get('title') or '').lower():
                        artist = ''  # Already in the name, or e.g. a "Stereo Love - Topic" channel; the ID is more useful
                    for suffix in filter(None, (artist, entry['id'])):
                        stem = stem_for(entry, f"{self.base_name(entry, naming)} ({suffix})")
                        if stem.lower() not in used:
                            break
                    used.add(stem.lower())
                    stems[entry['id']] = stem
        return stems

    # ---- resolving URLs ----

    def fetch(self, url):
        """Returns (playlist title or None, entries). A single video comes back as a one-entry list."""
        with yt_dlp.YoutubeDL({**self.base_opts(), 'extract_flat': 'in_playlist'}) as ydl:
            info = ydl.extract_info(url, download=False)
        if 'entries' in info:
            return info.get('title'), [e for e in info['entries'] or [] if e]
        return None, [info]

    def resolve(self, url, folder):
        """Returns (output_dir, entries) for one URL. A playlist goes into a subfolder named after it,
        a single video into the output folder itself, unless `folder` says otherwise."""
        if self.halted():
            return None, []
        try:
            playlist_title, entries = self.fetch(url)
        except DownloadError as err:
            if is_rate_limit(err):
                self.rate_limited.set()
            self.log(f"Couldn't fetch {url}: {clean_error(err)}")
            return None, []
        sub = folder or (sanitize_filename(playlist_title).strip() if playlist_title else '')
        output_dir = os.path.normpath(os.path.join(self.opts.output, sub))
        if playlist_title:
            self.log(f'Playlist "{playlist_title}": {len(entries)} items -> {output_dir}')
        else:
            self.log(f"Video: {entries[0].get('title') or url}")
        return output_dir, entries

    def plan(self, items):
        """Resolves all URLs and returns the jobs. Entries are grouped per output folder first, so the
        same video listed twice is downloaded once and unrelated videos never fight over one file name."""
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as pool:
            resolved = list(pool.map(lambda item: self.resolve(*item), items))

        by_dir = defaultdict(dict)  # output_dir -> {video id: entry}, in listing order
        for output_dir, entries in resolved:
            for entry in entries:
                by_dir[output_dir].setdefault(entry['id'], entry)

        other_naming = 'title' if self.opts.naming == 'title_channel' else 'title_channel'
        jobs = []
        for output_dir, entries in by_dir.items():
            os.makedirs(output_dir, exist_ok=True)
            stems = self.assign_stems(list(entries.values()), output_dir)
            other = self.assign_stems(list(entries.values()), output_dir, other_naming)
            jobs += [Job(e, stems[e['id']], output_dir, other[e['id']]) for e in entries.values()]
        return jobs

    def find_renames(self, jobs):
        """Missing files that were already downloaded under the other naming style, as
        (new stem, old path, new path)."""
        taken = {job.stem.lower() for job in jobs}  # Files that belong to another video under the new naming
        renames = []
        for job in jobs:
            old = job.other_stem
            if old.lower() in taken or self.is_complete(job.stem) or not self.is_complete(old):
                continue
            path = self.finished_file(old)
            renames.append((job.stem, path, job.stem + path[len(old):]))  # Keeps a " [1080p].mp4" tag
        return renames

    def offer_renames(self, jobs):
        """Asks whether files downloaded under the other naming style should be renamed instead of
        downloaded again, and renames them if so."""
        renames = self.find_renames(jobs)
        if not renames:
            return
        reply = queue.Queue()
        self.emit(AskRename([(old, new) for _, old, new in renames], reply))
        answer = None
        while answer is None and not self.stop_requested.is_set():
            try:
                answer = reply.get(timeout=0.25)
            except queue.Empty:
                pass
        if not answer:
            return
        done = 0
        for stem, old, new in renames:
            try:
                self.remove_partials(stem)  # An unfinished download under the new name is no longer needed
                os.rename(old, new)
                done += 1
            except OSError as err:
                self.log(f"Couldn't rename {old}: {err}")
        self.log(f"Renamed {done} file(s) to the new naming style.")

    # ---- downloading ----

    def progress_hook(self, d):
        if self.stop_requested.is_set():
            raise DownloadCancelled()
        key = threading.current_thread().name
        if d['status'] == 'finished':
            self.emit(FileProcessing(key))
        elif d['status'] == 'downloading':
            now = time.monotonic()
            if now - getattr(self._local, 'last', 0) >= PROGRESS_INTERVAL:
                self._local.last = now
                self.emit(FileProgress(key, d.get('downloaded_bytes') or 0,
                                       d.get('total_bytes') or d.get('total_bytes_estimate'),
                                       d.get('speed'), d.get('eta')))

    def download(self, video_id, stem):
        opts = {
            **self.base_opts(),
            **self.format_opts(),
            'outtmpl': stem.replace('%', '%%') + '.%(ext)s',
            'progress_hooks': [self.progress_hook],
            'sleep_interval': 1,       # Small random pause before each download to avoid getting rate-limited
            'max_sleep_interval': 3,
        }
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(f"https://www.youtube.com/watch?v={video_id}")
        if self.video_tagged():
            self.add_resolution_tag(info, stem)

    @staticmethod
    def add_resolution_tag(info, stem):
        # "p" is the shorter side, as YouTube labels it (a 1080x1920 Short is 1080p)
        sides = [s for s in (info.get('width'), info.get('height')) if s]
        path = stem + '.mp4'
        if sides and os.path.exists(path):
            os.replace(path, f"{stem} [{min(sides)}p].mp4")

    def try_download(self, video_id, stem):
        """Returns None on success, otherwise the error. Rate-limit errors are raised instead."""
        try:
            self.download(video_id, stem)
            return None
        except DownloadError as err:
            if is_rate_limit(err):
                raise
            return err

    def find_alternative(self, entry):
        """Topic / "Artist - Topic" tracks are sometimes blocked under their own ID, while YouTube Music
        silently plays another upload of the same song. Search for that upload, matching on duration."""
        query = f"{entry_artist(entry)} {entry.get('title', '')}".strip()
        want = entry.get('duration')
        searches = [
            f"https://music.youtube.com/search?q={urllib.parse.quote(query)}#songs",
            f"ytsearch{SEARCH_CANDIDATES}:{query}",
        ]
        opts = {**self.base_opts(), 'extract_flat': 'in_playlist', 'playlistend': SEARCH_CANDIDATES}
        with yt_dlp.YoutubeDL(opts) as ydl:
            def extract(url):
                if self.stop_requested.is_set():
                    raise DownloadCancelled()
                try:
                    return ydl.extract_info(url, download=False)
                except DownloadError as err:
                    if is_rate_limit(err):
                        raise
                    return None

            for search in searches:
                for cand in (extract(search) or {}).get('entries') or []:
                    if not cand or cand.get('id') == entry['id']:
                        continue
                    if want is None:
                        return cand['id']
                    info = extract(f"https://www.youtube.com/watch?v={cand['id']}")
                    if info and info.get('duration') and abs(info['duration'] - want) <= DURATION_TOLERANCE:
                        return cand['id']
        return None

    def download_with_fallback(self, entry, stem, title):
        error = self.try_download(entry['id'], stem)
        if error and self.has_partials(stem):
            # Leftovers from an interrupted run (e.g. a finished .part that couldn't be renamed, which
            # makes resuming fail with HTTP 416, or a corrupt .webm). Start this one from scratch.
            self.remove_partials(stem)
            error = self.try_download(entry['id'], stem)
        if not error:
            return 'ok', title

        if not entry.get('title'):
            # Nothing to search for; guessing would download the wrong song
            return 'failed', f'{title} (unavailable, no title to search for)'

        alt_id = self.find_alternative(entry)
        if not alt_id:
            return 'failed', f'{title} ({entry["id"]}): {clean_error(error)}; no alternative with the same length found'
        self.remove_partials(stem)  # Don't let the alternative "resume" the original video's partial file
        self.download(alt_id, stem)
        return 'ok', f'{title}  (unavailable, used alternative {alt_id})'

    def process(self, job):
        entry = job.entry
        title = entry.get('title') or entry['id']
        if title in UNAVAILABLE_TITLES:
            return 'failed', f'{title} ({entry["id"]})'
        if self.is_complete(job.stem):
            return 'skipped', title
        if self.halted():
            return 'pending', title

        key = threading.current_thread().name
        self.emit(FileStarted(key, title))
        try:
            return self.download_with_fallback(entry, job.stem, title)
        except DownloadCancelled:
            return 'pending', title
        except DownloadError as err:
            if is_rate_limit(err):
                self.rate_limited.set()
                return 'pending', title
            raise
        finally:
            self.emit(FileEnded(key))

    def check_cookies(self):
        """Reads the browser's cookies once up front, so a problem shows up as one clear message
        instead of every download failing."""
        if self.opts.cookies == 'none':
            return True
        try:
            extract_cookies_from_browser(self.opts.cookies, logger=SilentLogger())
            return True
        except Exception as err:
            self.log(f"Couldn't read cookies from {self.opts.cookies}: {clean_error(err)}\n"
                     "Close that browser and try again, pick another browser under Cookies, or choose 'none'.")
            return False

    def run(self, items):
        """Downloads everything behind `items` ((url, folder) pairs). Blocks until done or stopped."""
        try:
            if self.check_cookies():
                self._run(items)
        finally:
            self.emit(Finished())

    def _run(self, items):
        self.emit(Phase(f"Fetching {len(items)} URL(s)..."))
        jobs = self.plan(items)
        self.offer_renames(jobs)
        to_download = sum(not self.is_complete(job.stem) for job in jobs)
        self.log(f"\n{len(jobs)} item(s) in total, {len(jobs) - to_download} already downloaded, "
                 f"{to_download} to download.\n")
        self.emit(Started(len(jobs), to_download))

        counts = dict.fromkeys(('ok', 'skipped', 'failed', 'pending'), 0)
        failed = defaultdict(list)  # output_dir -> messages
        with ThreadPoolExecutor(max_workers=max(1, self.opts.workers), thread_name_prefix='dl') as pool:
            futures = {pool.submit(self.process, job): job for job in jobs}
            for done, future in enumerate(as_completed(futures), 1):
                job = futures[future]
                try:
                    status, msg = future.result()
                except Exception as err:
                    status, msg = 'failed', f"{job.entry.get('title')} ({job.entry['id']}): {clean_error(err)}"
                counts[status] += 1
                if status == 'failed':
                    failed[job.output_dir].append(msg)
                if status in ('ok', 'failed'):
                    self.log(f"[{done}/{len(jobs)}] {status.upper():6} {msg}")
                self.emit(ItemDone(status))

        for output_dir, msgs in failed.items():
            path = os.path.join(output_dir, 'failed.txt')
            with open(path, 'w', encoding='utf-8') as f:
                f.write('\n'.join(msgs))
            self.log(f"Failed items listed in {path}")

        self.log(f"\nDownloaded: {counts['ok']}, already had: {counts['skipped']}, failed: {counts['failed']}"
                 + (f", left for later: {counts['pending']}" if counts['pending'] else ''))
        if self.rate_limited.is_set():
            self.log("YouTube rate-limited this IP, so the rest was left for later. Wait a while (an hour or so) "
                     "and start again; finished files are skipped. If it keeps happening, use fewer parallel "
                     "downloads and make sure you're logged into YouTube in the browser chosen under Cookies.")
        elif self.stop_requested.is_set():
            self.log("Stopped. Start again to resume where you left off.")
