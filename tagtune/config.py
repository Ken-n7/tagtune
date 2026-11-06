"""Paths, limits and constants."""
import logging.handlers
from pathlib import Path
from urllib.parse import urlencode

OUT_DIR = Path.home() / "Music" / "tagtune"
# each job builds its files in its own hidden ".tmp-…" folder inside OUT_DIR and only moves finished
# songs out, so two open windows can never delete each other's work
_job_dirs = set()
CONFIG = Path.home() / ".config" / "tagtune" / "config.json"
LOG_FILE = Path.home() / ".local" / "state" / "tagtune" / "tagtune.log"
USER_AGENT = "TagTune/1.0 (personal desktop app)"
YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
PLAYLIST_CONFIRM = 25
MIN_FREE_BYTES = 500 * 1024 * 1024
MAX_COVER_BYTES = 5 * 1024 * 1024
HTTP_TIMEOUT = 10
DURATION_SLACK = 15  # seconds two copies of a song may differ by and still count as the same recording
# where to look for the official recording when an ordinary YouTube video is pasted, best first;
# each entry is (name, yt-dlp search for a query, results are always full songs), so another site is
# one more line. Where results are always full songs, a shorter match is taken as the song without a
# music video's skits; elsewhere a shorter match could be a preview clip, so lengths must agree.
OFFICIAL_SOURCES = (
    ("YouTube Music", lambda q: "https://music.youtube.com/search?" + urlencode({"q": q}) + "#songs", True),
    ("SoundCloud", lambda q: f"scsearch5:{q}", False),
)

log = logging.getLogger("tagtune")
