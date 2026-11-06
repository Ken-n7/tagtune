# TagTune

A small Linux desktop app: paste a YouTube or YouTube Music link and get a properly tagged MP3 in `~/Music/tagtune`.

- Finds the official recording (YouTube Music, then SoundCloud) when you paste an ordinary music video, so you get the song instead of the video's skits and intros.
- Fills in artist, title, album, year, track number and cover art from MusicBrainz, iTunes or Deezer.
- Handles playlists: each playlist's songs go in their own folder named after it (it asks first if there are more than 25 songs).
- Saves to `~/Music/tagtune` by default; pick another folder with "Change folder…".
- Light and dark themes that follow your system.

## Requirements

- Python 3.9+ with Tk (`python3-tk`)
- [ffmpeg](https://ffmpeg.org/)
- Node.js or Deno (recommended: some YouTube downloads need a JavaScript runtime)
- Python packages: `yt-dlp`, `mutagen`

## Setup

On Debian/Ubuntu:

```sh
sudo apt install python3 python3-tk python3-venv ffmpeg nodejs
git clone https://github.com/Ken-n7/tagtune.git
cd tagtune
python3 -m venv .venv
.venv/bin/pip install -U yt-dlp mutagen
```

## Usage

```sh
.venv/bin/python -m tagtune
```

Paste a link and press Download. Songs are saved to `~/Music/tagtune` unless you choose another folder with "Change folder…" (remembered between runs). A playlist gets its own subfolder.

Tick "Download exactly this video" to skip the search for the official version.

If downloads start failing, YouTube has probably changed something. Click "Update downloader" in the app, or update yt-dlp by hand:

```sh
.venv/bin/pip install -U yt-dlp
```

Run the built-in checks with `.venv/bin/python -m tagtune --selftest`.

Logs are written to `~/.local/state/tagtune/tagtune.log`.

## Note

Only download content you have the right to download.
