# YouTube Downloader

A small desktop app (Tkinter + [yt-dlp](https://github.com/yt-dlp/yt-dlp)) that downloads YouTube
videos, YouTube Music tracks and whole playlists as MP3 or MP4.

## Features

- MP3 (128–320 kbps) or MP4 (up to 2160p, H.264/AAC where available)
- Paste URLs one per line, or load a CSV of playlists
- Playlists are saved into a subfolder named after the playlist
- Already-downloaded files are skipped, and interrupted downloads resume where they left off
- File naming: `Title` or `Title - Channel`, optionally with the quality (`[320kbps]`, `[1080p]`)
- Switching the naming style offers to rename the files you already have instead of downloading them again
- Videos that share a title (covers, remixes, re-uploads) get distinct file names
- Unavailable tracks are replaced with another upload of the same song, matched by length
- Title/artist tags written into the files
- Uses your browser's YouTube login (cookies) to avoid bot checks and reach age-restricted videos
- Parallel downloads, per-file and overall progress with speed and ETA
- Stops cleanly when YouTube rate-limits you; start again later to continue

## Requirements

- Python 3.10 or newer
- [ffmpeg](https://ffmpeg.org/) (with ffprobe) on your PATH, e.g. `winget install ffmpeg`
- Optional but recommended: [Node.js](https://nodejs.org/) on your PATH, which lets yt-dlp solve
  YouTube's JavaScript challenges

## Setup

```sh
pip install -r requirements.txt
python main.py
```

Keep yt-dlp up to date (`pip install -U yt-dlp`); YouTube changes often and old versions stop working.

## Usage

1. Paste one or more URLs (videos, music tracks or playlists), or click **Load CSV...**.
2. Pick the format, quality, naming style and output folder.
3. Click **Start download** (or press Ctrl+Enter).

Your options are remembered in `settings.json` between runs.

### CSV format

Either a plain list of URLs in the first column, or a header row with a `url` column and an optional
`folder` column naming the subfolder of the output folder to save into (`.` saves straight into the
output folder). Blank rows and rows starting with `#` are ignored.

```csv
url,folder
https://youtube.com/playlist?list=...,Road Trip
https://youtube.com/playlist?list=...,.
```

### Cookies

Choose the browser you're logged into YouTube with under **Cookies from**. Some browsers lock their
cookie database while running; if reading them fails, close the browser or pick another one. Choose
`none` to download without logging in.

### Rate limits

YouTube blocks IPs that download too much too fast. Keep parallel downloads at 1–3. When you do get
rate-limited, the remaining items are left for later: wait an hour or so and start again, and
finished files are skipped.

Items that failed are listed in `failed.txt` in their output folder.

## Files

| File | Purpose |
| --- | --- |
| `main.py` | Entry point |
| `gui.py` | Tkinter interface |
| `downloader.py` | Download engine (no UI code; reports progress through events) |
