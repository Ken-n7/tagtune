#!/usr/bin/env python3
"""SongGrab: paste a YouTube link, get a tagged MP3 in ~/Music/songgrab."""
import glob
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from itertools import chain
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

OUT_DIR = Path.home() / "Music" / "songgrab"
# each job builds its files in its own hidden ".tmp-…" folder inside OUT_DIR and only moves finished
# songs out, so two open windows can never delete each other's work
_job_dirs = set()
CONFIG = Path.home() / ".config" / "songgrab" / "config.json"
LOG_FILE = Path.home() / ".local" / "state" / "songgrab" / "songgrab.log"
USER_AGENT = "SongGrab/1.0 (personal desktop app)"
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

log = logging.getLogger("songgrab")


# ---------- validation and text helpers ----------

def validate_url(text):
    """Return (url, None) for a usable YouTube link, else (None, message)."""
    text = text.strip()
    if not text:
        return None, "Paste a YouTube link first."
    if "://" not in text:
        text = "https://" + text
    try:
        u = urlparse(text)
        host = (u.hostname or "").lower()
    except ValueError:
        return None, "That doesn't look like a link."
    if u.scheme not in ("http", "https") or re.search(r"\s", text) or "." not in host:
        return None, "That doesn't look like a link. Copy the address from YouTube and paste it here."
    if host not in YT_HOSTS:
        return None, f"Only YouTube and YouTube Music links work here ({host} isn't one)."
    q = parse_qs(u.query)
    has_id = (
        (host == "youtu.be" and len(u.path) > 1)
        or q.get("v")
        or q.get("list")
        or re.match(r"/(shorts|live|embed|browse)/[\w-]+", u.path)
    )
    if not has_id:
        return None, "That link doesn't point to a song or playlist. Open the song and copy its link."
    return text, None


# A bracket is dropped from a title only if every word in it is filler: "(Official Music Video)",
# "[4K Remaster]". Anything else names a different recording and must stay: "(Live Video)",
# "(8D Audio)", "(Acoustic Video)", "(Official Cover Video)".
FILLER = {"official", "oficial", "officiel", "music", "video", "vídeo", "videoclip", "clip", "audio", "lyric", "lyrics",
          "visualizer", "visualiser", "hd", "uhd", "hq", "4k", "mv", "m", "v", "remaster", "remastered", "full", "with"}
BRACKET = re.compile(r"\s*[\(\[]([^\)\]]*)[\)\]]")
# a video title that says the song is a cover: "(X Cover)", "cover by X", "... cover"
COVER = re.compile(r"[\(\[][^\)\]]*\bcover\b|\bcover(s|ed)?\s+(by|of)\b|\bcover\s*$", re.I)


def clean_title(title):
    def drop_filler(m):
        words = re.findall(r"\w+", m.group(1).casefold())
        filler = any(w in FILLER for w in words) and all(w in FILLER or re.fullmatch(r"\d+p?", w) for w in words)
        return "" if filler else m.group(0)

    cleaned = re.sub(r"\s+(HD|HQ|4K)$", "", BRACKET.sub(drop_filler, title).strip())
    return cleaned or title.strip()


def file_name(artist, title):
    """Safe "Artist - Title" for a file: never hidden, and short enough for any filesystem."""
    from yt_dlp.utils import sanitize_filename

    name = sanitize_filename(f"{artist} - {title}").lstrip(". ") or "song"
    return name.encode()[:200].decode(errors="ignore").rstrip(". ") or "song"


def guess_tags(info):
    """(artist, title, album) from yt-dlp info, falling back to the video title and channel."""
    artist = (info.get("artists") or [None])[0] or info.get("artist")
    title = info.get("track") or info.get("title") or "Unknown"
    channel = CHANNEL_SUFFIX.sub("", info.get("uploader") or info.get("channel") or "") or "Unknown"
    cover = not info.get("track") and COVER.search(title)
    if cover:
        artist = channel  # a cover belongs to whoever performs it, not the artist named in the title
    left, sep, right = title.partition(" - ")
    # "Artist - Song" video titles, and track names that repeat the artist (common on SoundCloud)
    if sep and (norm(left) == norm(artist) or not (info.get("track") or cover)):
        artist, title = artist or left, right
    return (artist or channel).strip(), clean_title(title), info.get("album")


def norm(s):
    return re.sub(r"\W+", "", (s or "").casefold())


CHANNEL_SUFFIX = re.compile(r"\s*(-\s*Topic|VEVO)$")
ARTIST_SPLIT = re.compile(r"\s*(?:,|&|\+|;|\bx\b|\band\b|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b|\bwith\b|\bvs\b\.?)\s*", re.I)


def artist_names(*credits):
    """Every comparable name in some artist credits: "A & B feat. C" -> {a, b, c, abfeatc}."""
    names = set()
    for credit in credits:
        credit = CHANNEL_SUFFIX.sub("", credit or "")
        names |= {norm(credit), *map(norm, ARTIST_SPLIT.split(credit))}
    return names - {""}


def same_artist(artist, *credits):
    """Whole names must agree: "Air" is not "Air Supply"."""
    return bool(artist_names(artist) & artist_names(*credits))


# a featuring credit, in brackets or trailing, and nothing after it: "(Live)" or "[Remix]" must survive
FEAT = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring|with)\b\.?\s[^\)\]]*[\)\]]"
                  r"|\s+(?:feat|ft|featuring)\b\.?\s+[^\(\[\-–|]*", re.I)


def title_key(text):
    """Comparable form of a title: "Song ft. X" and "Song (feat. X) [Official Video]" are the same song."""
    return norm(FEAT.sub("", clean_title(text or "")))


def same_title(cand, artist, title):
    """Does a search result carry this song's title (alone, or as "Artist - Title")?"""
    found = title_key(cand.get("track") or cand.get("title"))
    return bool(found) and found in (title_key(title), norm(artist) + title_key(title))


def same_song(cand, artist, title, duration, shorter_ok=False):
    """Is a search result the same recording: same title, same artist, same length?
    With shorter_ok, a shorter result also counts: the song without a music video's skits."""
    length = cand.get("duration")
    # ponytail: the one-third floor is a guess that keeps a full-album upload from being swapped for
    # its title track; ask YouTube Music for the video's linked song if this picks wrong versions
    return bool(
        duration and length
        and same_title(cand, artist, title)
        and same_artist(artist, *(cand.get("artists") or []), cand.get("artist"), cand.get("uploader"))
        and (abs(length - duration) <= DURATION_SLACK or (shorter_ok and duration / 3 <= length < duration))
    )


def image_mime(data):
    """MIME type if data is a sane JPEG/PNG, else None."""
    if not data or len(data) > MAX_COVER_BYTES:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    return None


class Problem(Exception):
    """Something the user should be told as is."""


def friendly(err):
    """(plain-words message, might_be_stale_downloader) for whatever stopped a song."""
    if isinstance(err, Problem):
        return str(err), False
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(err))
    low = msg.lower()
    known = (
        ("no space left", "The disk is full. Free some space and try again."),
        ("permission denied", f"Can't write to {OUT_DIR}. Check that folder's permissions."),
        ("read-only file system", f"Can't write to {OUT_DIR}. Check that folder's permissions."),
        ("private video", "This video is private."),
        ("confirm your age", "This video is age-restricted, so it can't be downloaded."),
        ("not available in your country", "This video is blocked in your country."),
        ("not a bot", "YouTube is asking for a sign-in right now. Wait a few minutes and try again."),
        ("playlist does not exist", "This playlist doesn't exist or is private."),
        ("unavailable", "This video is unavailable or was removed."),
        ("not available", "This video is unavailable or was removed."),
        ("removed", "This video is unavailable or was removed."),
    )
    for needle, text in known:
        if needle in low:
            return text, False
    if any(n in low for n in ("name resolution", "failed to resolve", "unreachable", "timed out", "connection re")):
        return "Couldn't reach YouTube. Check your internet connection and try again.", False
    first = re.sub(r"^(ERROR: )?(\[[^\]]+\] )?([\w-]+: )?", "", msg.strip().splitlines()[0] if msg.strip() else "Unknown error")
    if not type(err).__module__.startswith("yt_dlp"):  # our own bug or the system, not a stale downloader
        return f"Something went wrong: {first[:200]} (details are in the log)", False
    return f"{first[:200]} If this keeps happening, update the downloader.", True


# ---------- MusicBrainz ----------

_last_mb_call = 0.0


def http_get(url):
    """GET with a timeout; MusicBrainz calls are spaced 1s apart and retried twice when it is busy."""
    global _last_mb_call
    for attempt in (1, 2, 3):
        if "musicbrainz.org" in url:
            time.sleep(max(0.0, 1.1 - (time.monotonic() - _last_mb_call)))
            _last_mb_call = time.monotonic()
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=HTTP_TIMEOUT) as r:
                return r.read(MAX_COVER_BYTES + 1)
        except HTTPError as e:
            if e.code != 503 or attempt == 3:
                raise
            time.sleep(2 * attempt)


def plain_album(name):
    """Album name without edition notes: "The Fame Monster (Deluxe Edition)" -> "The Fame Monster"."""
    return re.sub(r"\s*[\(\[].*", "", name or "").strip()


# release kinds that are never a song's home album
SIDE_RELEASES = {"Compilation", "Live", "DJ-mix", "Remix", "Demo", "Mixtape/Street", "Interview", "Spokenword", "Audiobook"}


def pick_release(recordings, artist, title, album, duration):
    """Best (recording, release) from a MusicBrainz search, or None if nothing is trustworthy."""
    # ponytail: name-based ranking heuristic; add AcoustID fingerprinting if it mis-tags songs
    best = None
    for rec in recordings:
        credit = [a.get("name") for a in rec.get("artist-credit", [])]
        # MusicBrainz's score only ranks hits against each other, so check the facts instead
        if title_key(rec.get("title")) != title_key(title) or not same_artist(artist, *credit):
            continue
        if duration and rec.get("length") and abs(rec["length"] / 1000 - duration) > DURATION_SLACK:
            continue
        for rel in rec.get("releases", []):
            group = rel.get("release-group", {})
            plain = rel.get("status") == "Official" and not group.get("secondary-types")
            album_hit = bool(album) and norm(plain_album(rel.get("title"))) == norm(plain_album(album))
            if SIDE_RELEASES & set(group.get("secondary-types") or []) and not album_hit:
                continue  # "Now!6" or a live album is never a better tag than the one the source gave
            key = (
                not album_hit,
                not (plain and group.get("primary-type") == "Album"),
                not (plain and group.get("primary-type") in ("Single", "EP")),
                (rel.get("date") or "").ljust(10, "9"),  # earliest first; a bare year sorts after full dates in it
            )
            if best is None or key < best[0]:
                best = (key, rec, rel)
    return best and best[1:]


def mb_lookup(artist, title, album, duration):
    def quote(s):
        return s.replace("\\", " ").replace('"', " ")

    query = f'recording:"{quote(title)}" AND artist:"{quote(artist)}"'
    # popular songs sit on dozens of compilations, so ask for the source's album first
    by_album = [f'{query} AND release:"{quote(plain_album(album))}"'] if plain_album(album) else []
    for q in (*by_album, query):
        url = "https://musicbrainz.org/ws/2/recording?" + urlencode({"query": q, "limit": 25, "fmt": "json"})
        match = pick_release(json.loads(http_get(url)).get("recordings", []), artist, title, album, duration)
        if match:
            return match
    return None


def tags_from(rec, rel):
    def credit(parts):
        return "".join(a.get("name", "") + a.get("joinphrase", "") for a in parts or [])

    artist = credit(rec.get("artist-credit"))
    media = (rel.get("media") or [{}])[0]
    number = ((media.get("track") or [{}])[0].get("number") or "")
    track = number if number.isdigit() else ""
    if track and media.get("track-count"):
        track += f"/{media['track-count']}"
    return {
        "title": rec.get("title"),
        "artist": artist,
        "album": rel.get("title"),
        "albumartist": credit(rel.get("artist-credit")) or artist,
        "date": (rel.get("date") or rec.get("first-release-date") or "")[:4],
        "track": track,
        "mbid": rec.get("id"),
    }


def fetch_image(url):
    """(bytes, mime) for a cover image, or None. A cover is optional, so nothing here is fatal."""
    try:
        data = http_get(url) if url else None
    except Exception:  # 404, offline, timeout, cut-off transfer
        return None
    mime = image_mime(data)
    return (data, mime) if mime else None


def fetch_cover(rel):
    """(bytes, mime) for the release's front cover, then its release group's, else None."""
    ids = (("release", rel.get("id")), ("release-group", rel.get("release-group", {}).get("id")))
    return next(filter(None, (fetch_image(f"https://coverartarchive.org/{kind}/{mbid}/front-500")
                              for kind, mbid in ids if mbid)), None)


def musicbrainz_tags(artist, title, album, duration):
    match = mb_lookup(artist, title, album, duration)
    return match and (tags_from(*match), fetch_cover(match[1]))


def pick_track(cands, artist, title, album, duration):
    """Best hit from a music store's search, or None. Each candidate has title, artist, album, length
    and optionally date, single, side (a compilation). Same rules as MusicBrainz: the facts must agree."""
    best = None
    for c in cands:
        if title_key(c.get("title")) != title_key(title) or not same_artist(artist, c.get("artist")):
            continue
        if duration and c.get("length") and abs(c["length"] - duration) > DURATION_SLACK:
            continue
        album_hit = bool(album) and norm(plain_album(c.get("album"))) == norm(plain_album(album))
        if c.get("side") and not album_hit:
            continue
        key = (not album_hit, bool(c.get("single")), (c.get("date") or "").ljust(10, "9"))
        if best is None or key < best[0]:  # ties keep the store's own ranking
            best = (key, c)
    return best and best[1]


def itunes_tags(artist, title, album, duration):
    """Tags and cover from Apple's public search (no account; about 20 lookups a minute)."""
    url = "https://itunes.apple.com/search?" + urlencode({"term": f"{artist} {title}", "entity": "song", "limit": 25})
    cands = [{
        "title": r.get("trackName"), "artist": r.get("artistName"),
        "album": re.sub(r"\s+-\s+(Single|EP)$", "", r.get("collectionName") or ""),
        "length": (r.get("trackTimeMillis") or 0) / 1000, "date": r.get("releaseDate"),
        "single": (r.get("collectionName") or "").endswith(" - Single"),
        "side": r.get("collectionArtistName") == "Various Artists", "raw": r,
    } for r in json.loads(http_get(url)).get("results", [])]
    c = pick_track(cands, artist, title, album, duration)
    if not c:
        return None
    r = c["raw"]
    return {
        "title": c["title"], "artist": c["artist"], "album": c["album"],
        "albumartist": r.get("collectionArtistName") or c["artist"], "date": (c["date"] or "")[:4],
        "track": f"{r['trackNumber']}/{r['trackCount']}" if r.get("trackNumber") and r.get("trackCount") else "",
    }, fetch_image((r.get("artworkUrl100") or "").replace("100x100bb", "600x600bb"))


def deezer_tags(artist, title, album, duration):
    """Tags and cover from Deezer's public API (no account)."""
    url = "https://api.deezer.com/search?" + urlencode({"q": f"{artist} {title}", "limit": 25})
    cands = [{
        "title": r.get("title"), "artist": (r.get("artist") or {}).get("name"),
        "album": (r.get("album") or {}).get("title"), "length": r.get("duration"), "raw": r,
    } for r in json.loads(http_get(url)).get("data", [])]
    c = pick_track(cands, artist, title, album, duration)
    if not c:
        return None
    track = json.loads(http_get(f"https://api.deezer.com/track/{c['raw']['id']}"))
    disc = json.loads(http_get(f"https://api.deezer.com/album/{c['raw']['album']['id']}"))
    album_hit = bool(album) and norm(plain_album(c["album"])) == norm(plain_album(album))
    if disc.get("record_type") == "compile" and not album_hit:
        return None
    return {
        "title": c["title"], "artist": c["artist"], "album": c["album"],
        "albumartist": (disc.get("artist") or {}).get("name") or c["artist"],
        "date": (track.get("release_date") or disc.get("release_date") or "")[:4],
        "track": f"{track['track_position']}/{disc['nb_tracks']}" if track.get("track_position") and disc.get("nb_tracks") else "",
    }, fetch_image(c["raw"]["album"].get("cover_xl"))


# where tags and cover art come from, best first; the next one is asked when one is busy or has no match
TAG_SOURCES = (("MusicBrainz", musicbrainz_tags), ("iTunes", itunes_tags), ("Deezer", deezer_tags))


def write_tags(path, tags, cover=None):
    """Write ID3 tags; the file is restored from a backup if anything goes wrong."""
    from mutagen.id3 import APIC, ID3, TALB, TDRC, TIT2, TPE1, TPE2, TRCK, UFID, ID3NoHeaderError
    from mutagen.mp3 import MP3

    frames = {"title": TIT2, "artist": TPE1, "album": TALB, "albumartist": TPE2, "date": TDRC, "track": TRCK}
    backup = path.with_suffix(".bak")
    shutil.copy2(path, backup)
    try:
        length = MP3(path).info.length
        try:
            id3 = ID3(path)
        except ID3NoHeaderError:
            id3 = ID3()
        for key, frame in frames.items():
            if tags.get(key):
                id3.setall(frame.__name__, [frame(encoding=3, text=tags[key])])
        if tags.get("mbid"):
            id3.setall("UFID", [UFID(owner="http://musicbrainz.org", data=tags["mbid"].encode())])
        if cover:
            id3.setall("APIC", [APIC(encoding=3, mime=cover[1], type=3, desc="Cover", data=cover[0])])
        id3.save(path, v2_version=3)
        if abs(MP3(path).info.length - length) > 1:
            raise ValueError("audio length changed while tagging")
    except Exception:
        shutil.copy2(backup, path)
        raise
    finally:
        backup.unlink(missing_ok=True)


# ---------- download ----------

def find_tool(name, *fallbacks):
    return shutil.which(name) or next((p for p in fallbacks if os.access(p, os.X_OK)), None)


def find_ffmpeg():
    return find_tool("ffmpeg", str(Path.home() / ".local/bin/ffmpeg"))


def find_node():
    # the apps menu doesn't load nvm, so look there too (newest version first)
    nvm = sorted(
        glob.glob(str(Path.home() / ".nvm/versions/node/*/bin/node")),
        key=lambda p: [int(n) for n in re.findall(r"\d+", Path(p).parts[-3])],
        reverse=True,
    )
    return find_tool("node", *nvm)


def peak_db(path):
    """Loudest sample of an audio file in dBFS (above 0 means players will clip it), or None."""
    try:
        out = subprocess.run(
            [find_ffmpeg(), "-hide_banner", "-nostdin", "-i", str(path), "-map", "0:a:0", "-af",
             "astats=metadata=0:measure_overall=Peak_level:measure_perchannel=none", "-f", "null", "-"],
            capture_output=True, text=True, timeout=300,
        ).stderr
        return float(re.search(r"Peak level dB:\s*(-?[\d.]+)", out).group(1))
    except (OSError, subprocess.SubprocessError, AttributeError, ValueError, TypeError):
        return None


def preflight():
    """(fatal_message or None, [warnings]) checked once at startup."""
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not (OUT_DIR.is_dir() and os.access(OUT_DIR, os.W_OK)):
        return f"Can't save songs: {OUT_DIR} is missing or not writable.", []
    for stale in OUT_DIR.glob(".tmp*"):  # a live job keeps touching its folder, a crashed one doesn't
        if time.time() - stale.stat().st_mtime > 24 * 3600:
            shutil.rmtree(stale, ignore_errors=True)
    if not find_ffmpeg():
        return "ffmpeg wasn't found, so songs can't be converted. Install it with: sudo apt install ffmpeg", []
    warnings = []
    if not (find_node() or shutil.which("deno")):
        warnings.append("Node.js wasn't found, so some YouTube downloads may fail.")
    if shutil.disk_usage(OUT_DIR).free < MIN_FREE_BYTES:
        warnings.append("Less than 500 MB of disk space is free.")
    return None, warnings


def ydl_opts(hook, tmp):
    runtimes = {"deno": {}}
    if find_node():
        runtimes["node"] = {"path": find_node()}
    return {
        "format": "bestaudio/best",
        "outtmpl": str(Path(tmp) / "%(id)s.%(ext)s"),
        "noplaylist": True,  # a song link that also carries &list= gets the one song, not the playlist
        "extract_flat": "in_playlist",
        "writethumbnail": True,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"}],
        # centre-crop the thumbnail to a square so it works as cover art
        "postprocessor_args": {
            "thumbnailsconvertor+ffmpeg_o": ["-c:v", "mjpeg", "-vf", "crop='if(gt(ih,iw),iw,ih)':'if(gt(iw,ih),ih,iw)'"]
        },
        "ffmpeg_location": os.path.dirname(find_ffmpeg() or ""),
        "js_runtimes": runtimes,
        "socket_timeout": 20,
        "retries": 5,
        "fragment_retries": 5,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": logging.getLogger("yt-dlp"),
        "progress_hooks": [hook],
    }


def run_job(url, emit, cancel, confirm, exact=False):
    """Download and tag everything behind url. Runs on a worker thread; never raises.
    With exact, the audio always comes from the pasted link itself, never an official copy."""
    from yt_dlp import YoutubeDL
    from yt_dlp.postprocessor import EmbedThumbnailPP, FFmpegExtractAudioPP, FFmpegMetadataPP, PostProcessor
    from yt_dlp.utils import DownloadCancelled

    class PeakGuard(PostProcessor):
        """YouTube audio often decodes louder than full scale and its player turns it down; an MP3
        player just clips it. Turn such tracks down during conversion so the peak sits at -1 dB."""

        def run(self, info):
            if cancel.is_set():
                raise DownloadCancelled()
            peak = peak_db(info["filepath"])
            gain = min(0.0, -1.0 - peak) if peak is not None else 0.0
            log.info("peak %s dB, gain %.2f dB: %s", peak, gain, info.get("title"))
            args = self._downloader.params.setdefault("postprocessor_args", {})
            args["extractaudio+ffmpeg_o"] = ["-af", f"volume={gain:.2f}dB"] if gain else []
            return [], info

    counts = Counter()
    where = ""
    tmp = None
    # a YouTube Music link is already the official recording; anything else gets looked up
    lookup = not exact and urlparse(url).hostname != "music.youtube.com"

    def hook(d):
        if cancel.is_set():
            raise DownloadCancelled()
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            pct = d.get("downloaded_bytes", 0) / total * 100 if total else 0
            speed = f" · {d['speed'] / 1e6:.1f} MB/s" if d.get("speed") else ""
            emit("progress", pct, f"Downloading {pct:.0f}%{speed}{where}")
        elif d["status"] == "finished":
            emit("status", f"Converting to MP3…{where}")

    def fail(name, err):
        text, stale = friendly(err)
        counts["failed"] += 1
        emit("row", "fail", name, text)
        if stale:
            emit("stale")

    def official(ydl, info, artist, title):
        """Yield (info, site) for official copies of the same recording, best site first."""
        if not info.get("duration"):
            return
        for site, search, full_songs in OFFICIAL_SOURCES:
            if not full_songs and same_artist(artist, info.get("uploader")):
                continue  # the pasted video is the artist's own upload; another site's copy is no better
            try:
                found = ydl.extract_info(search(f"{artist} {title}"), download=False) or {}
                for entry in (found.get("entries") or [])[:5]:
                    if cancel.is_set():
                        raise DownloadCancelled()
                    if not entry or not same_title(entry, artist, title):
                        continue
                    if entry.get("duration") and abs(entry["duration"] - info["duration"]) > DURATION_SLACK:
                        continue  # e.g. SoundCloud's 30-second previews
                    cand = ydl.extract_info(entry.get("webpage_url") or entry["url"], download=False)
                    if cand.get("id") == info.get("id"):
                        return  # the pasted link already is the official one
                    if same_song(cand, artist, title, info["duration"], full_songs):
                        yield cand, site
                        break
            except DownloadCancelled:
                raise
            except Exception:
                log.exception("official lookup on %s failed", site)

    def download(ydl, src):
        """YouTube now and then refuses (HTTP 403) a link it has just handed out, so ask for a fresh one twice."""
        for pause in (3, 8, None):
            try:
                return ydl.process_ie_result(src, download=True)
            except DownloadCancelled:
                raise
            except Exception:
                if pause is None or not src.get("webpage_url") or cancel.is_set():
                    raise
                log.exception("download failed, retrying with a fresh link: %s", src["webpage_url"])
                time.sleep(pause)
                src = ydl.extract_info(src["webpage_url"], download=False)

    def grab(ydl, item):
        info = item if isinstance(item, dict) else ydl.extract_info(item, download=False)
        if info.get("is_live") or info.get("live_status") == "is_upcoming":
            raise Problem("This is a live stream, so there is no finished song to download.")
        sources = [(info, "")]
        # no lookup for official "Topic" uploads, or for covers (it would find the original instead)
        if lookup and not (info.get("uploader") or "").endswith(" - Topic") and not COVER.search(info.get("title") or ""):
            emit("status", f"Looking for the official version…{where}")
            sources = chain(official(ydl, info, *guess_tags(info)[:2]), sources)
        for src, site in sources:  # official copies first, the pasted video last
            artist, title, album = guess_tags(src)
            name = f"{artist} - {title}"
            final = OUT_DIR / (file_name(artist, title) + ".mp3")
            if final.exists():
                counts["skipped"] += 1
                emit("row", "skip", name, "Already downloaded")
                return
            try:
                done = download(ydl, src)
                break
            except DownloadCancelled:
                raise
            except Exception:
                if src is info:
                    raise
                log.exception("official copy on %s failed, trying the next source", site)
        info = src
        path = Path(done["requested_downloads"][0]["filepath"])
        emit("status", f"Finding tags…{where}")
        kind, note = "keep", f"Kept {'SoundCloud' if site == 'SoundCloud' else 'YouTube'} tags"
        try:
            # worst case leaves the tags and square thumbnail yt-dlp already embedded
            write_tags(path, {"title": title, "artist": artist})
        except Exception:
            log.exception("tagging failed for %s", name)
        for source, find_tags in TAG_SOURCES:
            if cancel.is_set():
                break
            try:
                found = find_tags(artist, title, album, info.get("duration"))
                if found:
                    write_tags(path, *found)
                    kind, note = "ok", f"Tagged from {source}"
                    break
            except Exception:  # busy, offline, odd answer: ask the next one
                log.exception("%s lookup failed for %s", source, name)
        os.replace(path, final)
        counts["saved"] += 1
        counts["kept"] += kind == "keep"
        emit("row", kind, name, note + (f" · official audio from {site}" if site else ""))

    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".tmp-", dir=OUT_DIR)
        _job_dirs.add(tmp)
        emit("status", "Checking link…")
        with YoutubeDL(ydl_opts(hook, tmp)) as ydl:
            # each one is given ydl up front: they look up ffmpeg (ffmpeg_location) when created
            for pp in (PeakGuard(ydl), FFmpegExtractAudioPP(ydl, preferredcodec="mp3", preferredquality="0"),
                       FFmpegMetadataPP(ydl), EmbedThumbnailPP(ydl)):
                ydl.add_post_processor(pp)
            try:
                first = ydl.extract_info(url, download=False)
            except DownloadCancelled:
                raise
            except Exception as e:
                log.exception("could not read %s", url)
                fail(url, e)
                first = None
            items = []  # (what to download, what to call it if it fails)
            if first and first.get("_type") == "playlist":
                items = [(e.get("url") or e.get("webpage_url"), e.get("title") or e.get("url") or url)
                         for e in first.get("entries") or [] if e and (e.get("url") or e.get("webpage_url"))]
                if not items:
                    fail(url, Problem("This playlist is empty."))
                if len(items) > PLAYLIST_CONFIRM and not confirm(len(items)):
                    raise DownloadCancelled()
            elif first:
                items = [(first, first.get("title") or url)]
            for i, (item, label) in enumerate(items, 1):
                if cancel.is_set():
                    raise DownloadCancelled()
                where = f" · track {i} of {len(items)}" if len(items) > 1 else ""
                emit("status", f"Checking…{where}")
                try:
                    grab(ydl, item)
                except DownloadCancelled:
                    raise
                except Exception as e:
                    log.exception("failed on %s", label)
                    fail(label, e)
        summary = " · ".join(
            text
            for n, text in (
                (counts["saved"], f"{counts['saved']} saved"),
                (counts["kept"], f"{counts['kept']} kept original tags"),
                (counts["skipped"], f"{counts['skipped']} already downloaded"),
                (counts["failed"], f"{counts['failed']} failed"),
            )
            if n
        )
        emit("done", summary or "Nothing to download")
    except DownloadCancelled:
        emit("done", "Cancelled" + (f" · {counts['saved']} saved" if counts["saved"] else ""))
    except Exception as e:
        log.exception("job crashed")
        emit("row", "fail", url, friendly(e)[0])
        emit("done", "Failed")
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)
            _job_dirs.discard(tmp)


# ---------- window ----------

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

        root.title("SongGrab")
        root.geometry("640x500")
        root.minsize(560, 440)
        f = ttk.Frame(root, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(0, weight=1)
        f.rowconfigure(7, weight=1)

        ttk.Label(f, text="SongGrab", style="Title.TLabel").grid(row=0, column=0, sticky="w")
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


# ---------- entry points ----------

def selftest():
    ok = lambda text: validate_url(text)[1] is None  # noqa: E731
    assert ok("https://music.youtube.com/watch?v=abc123&list=RDxyz")
    assert ok("https://youtu.be/abc123") and ok("youtube.com/playlist?list=PL123")
    assert ok("  https://www.youtube.com/shorts/abc-_1  ")
    assert not ok("") and not ok("hello") and not ok("hello world.com")
    assert not ok("https://soundcloud.com/a/b") and not ok("ftp://youtube.com/watch?v=a")
    assert not ok("https://www.youtube.com/") and not ok("https://youtu.be/")

    assert clean_title("One More Time (Official Video)") == "One More Time"
    assert clean_title("Song [4K Remaster] (feat. X)") == "Song (feat. X)"
    assert clean_title("(Official Audio)") == "(Official Audio)"
    assert clean_title("Dear Maria, Count Me In HD") == "Dear Maria, Count Me In" and clean_title("HD") == "HD"
    assert guess_tags({"title": "Daft Punk - One More Time (Official Video)", "uploader": "DaftPunkVEVO"}) == (
        "Daft Punk", "One More Time", None)
    assert guess_tags({"title": "x", "track": "T", "artists": ["A", "B"], "album": "Al"}) == ("A", "T", "Al")
    assert guess_tags({"track": "All Time Low - Dear Maria", "artists": ["All Time Low"]})[:2] == ("All Time Low", "Dear Maria")
    assert guess_tags({"track": "Song - Remastered 2011", "artist": "Band"})[:2] == ("Band", "Song - Remastered 2011")
    cover = lambda title, channel: guess_tags({"title": title, "uploader": channel})[:2]  # noqa: E731
    assert cover("All Time Low - Dear Maria, Count Me In (Vocal Cover)", "Riley Mahon") == (
        "Riley Mahon", "All Time Low - Dear Maria, Count Me In (Vocal Cover)"), "a cover is credited to its performer"
    assert cover("Lady Gaga - Paparazzi (Official Cover Video)", "Some Singer") == (
        "Some Singer", "Lady Gaga - Paparazzi (Official Cover Video)"), "and never reads as the original"
    assert cover("DIAMANTE - Paparazzi (Official Cover)", "DIAMANTE") == ("DIAMANTE", "Paparazzi (Official Cover)")
    assert cover("Dear Maria (All Time Low Cover)", "I Belong to the Zoo") == ("I Belong to the Zoo", "Dear Maria (All Time Low Cover)")
    assert cover("Daft Punk - Discovery (Official Video)", "Daft Punk") == ("Daft Punk", "Discovery"), "'Discovery' is no cover"
    assert COVER.search("Song - cover by X") and COVER.search("Song acoustic cover") and not COVER.search("Cover Me")
    assert guess_tags({"title": "Just A Title", "uploader": "Some Band - Topic"}) == ("Some Band", "Just A Title", None)

    def rel(title, kind, secondary=(), date="2001", status="Official"):
        return {"id": title, "title": title, "status": status, "date": date,
                "release-group": {"primary-type": kind, "secondary-types": list(secondary)}}

    def rec(title, releases, score=100, length=320000, artist="Daft Punk"):
        return {"id": title, "title": title, "score": score, "length": length,
                "artist-credit": [{"name": artist}], "releases": releases}

    comp, album, single = rel("Now!6", "Album", ["Compilation"], "2000"), rel("Discovery", "Album"), rel("OMT", "Single", date="2000")
    recs = [rec("One More Time", [comp, single, album])]
    pick = lambda **kw: pick_release(recs, **{"artist": "daft punk", "title": "One More Time", "album": None, "duration": 320, **kw})  # noqa: E731
    assert pick()[1] is album, "studio album must beat compilation and single"
    assert pick(album="now!6")[1] is comp, "YouTube's album name must win"
    assert pick(album="Discovery (Deluxe Edition)")[1] is album, "edition notes are ignored"
    assert pick_release([rec("One More Time", [comp])], "Daft Punk", "One More Time", "Discovery", 320) is None, \
        "a compilation never replaces the source's album"
    assert pick(duration=200) is None, "wrong length is rejected"
    assert pick(artist="Someone Else") is None, "wrong artist is rejected"
    assert pick_release([rec("One More Time (live)", [album]), rec("One More Time", [single])],
                        "Daft Punk", "One More Time", None, 320)[1] is single, "other versions are never used"
    assert pick_release([rec("One More Time (feat. Romanthony)", [album])], "Daft Punk", "One More Time", None, 320)
    assert tags_from(recs[0], {**album, "media": [{"track-count": 14, "track": [{"number": "1"}]}]})["track"] == "1/14"

    song = dict(artist="All Time Low", title="Dear Maria, Count Me In", duration=185)
    ytm = {"track": "Dear Maria, Count Me In", "artists": ["All Time Low"], "uploader": "All Time Low", "duration": 183}
    label = {"title": "All Time Low - Dear Maria, Count Me In", "artists": ["All Time Low"], "uploader": "Hopeless Records", "duration": 182.9}
    assert same_song(ytm, **song) and same_song(label, **song), "official copies are accepted"
    assert not same_song({**ytm, "duration": 30.0}, **song), "30-second preview is rejected"
    assert not same_song({**ytm, "track": "Dear Maria, Count Me In (ATL's Version)"}, **song), "other version is rejected"
    assert not same_song({"title": "All Time Low Dear Maria", "uploader": "some_fan", "duration": 185}, **song)
    assert not same_song({**label, "artists": ["EricArchive"], "uploader": "TTMZ41"}, **song), "re-upload is rejected"
    assert not same_song(ytm, **{**song, "duration": None}), "unknown length can't be verified"
    video = {**song, "duration": 500}  # music video with skits, song is 183 s
    assert same_song(ytm, **video, shorter_ok=True), "song without the video's skits is accepted"
    assert not same_song(ytm, **video), "but not where a shorter result could be a preview"
    assert not same_song(ytm, **{**song, "duration": 3300}, shorter_ok=True), "full-album upload keeps its own audio"
    assert not same_song({**ytm, "duration": 400}, **song, shorter_ok=True), "a longer result is a different version"
    assert same_title({"track": "Telephone (feat. Beyoncé)"}, "Lady Gaga", "Telephone ft. Beyoncé")
    assert not same_title({"track": "Telephone (Remix)"}, "Lady Gaga", "Telephone")

    store = lambda **kw: {"title": "Telephone (feat. Beyoncé)", "artist": "Lady Gaga", "album": "The Fame Monster", "length": 221, **kw}  # noqa: E731
    want = dict(artist="Lady Gaga", title="Telephone", album=None, duration=220)
    remix, various = store(title="Telephone (Passion Pit Remix)"), store(album="Now 75", side=True)
    single, studio = store(album="Telephone", single=True, date="2010"), store(date="2009-11-18")
    assert pick_track([remix, various, single, studio], **want) is studio, "album beats single; remix and compilation are out"
    assert pick_track([remix, various], **want) is None and pick_track([studio], **{**want, "duration": 400}) is None
    assert pick_track([various, studio], **{**want, "album": "Now 75"}) is various, "the source's own album always wins"
    assert pick_track([store(artist="Someone Else")], **want) is None

    assert image_mime(b"\xff\xd8\xff\xe0 jpeg") == "image/jpeg" and image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert image_mime(b"") is None and image_mime(b"<html>404") is None
    assert image_mime(b"\xff\xd8\xff" + b"0" * MAX_COVER_BYTES) is None
    assert friendly("ERROR: [youtube] abc: Private video. Sign in")[0] == "This video is private."
    assert friendly("ERROR: [youtube] abc: This video is unavailable") == ("This video is unavailable or was removed.", False)
    from yt_dlp.utils import DownloadError

    assert friendly(DownloadError("ERROR: [youtube] abc: Something new broke"))[1] is True, "yt-dlp trouble may be a stale downloader"
    assert friendly(KeyError("x"))[1] is False and friendly(Problem("Plain words."))  == ("Plain words.", False)
    assert friendly(OSError(28, "No space left on device"))[0].startswith("The disk is full")

    # different recordings keep their version note, so they are never mistaken for the studio song
    for kept in ("Hello (Live Video)", "Hello (8D AUDIO)", "Hello (Acoustic Video)", "Hello (Lyric Video Remix)", "Hello (Live)"):
        assert clean_title(kept) == kept, kept
    for filler in ("Hello (Official Music Video)", "Hello [Official Visualizer]", "Hello (1080p HD)", "Hello (Video Oficial)"):
        assert clean_title(filler) == "Hello", filler
    assert title_key("Telephone ft. Beyoncé") == title_key("Telephone (feat. Beyoncé)") == title_key("Telephone")
    assert title_key("Hello ft. Someone (Live at Wembley)") != title_key("Hello")
    assert title_key("Hello (feat. X) [Remix]") != title_key("Hello")

    assert not same_artist("Air", "Air Supply") and not same_artist("A", "A Day to Remember") and not same_artist("!!!", "")
    assert same_artist("Lady Gaga", "LadyGagaVEVO") and same_artist("Lady Gaga & Beyoncé", "Beyoncé")
    assert same_artist("Florence and the Machine", "Florence + The Machine") and same_artist("Band", "Band - Topic")

    assert not file_name(".38 Special", "Hold On").startswith(".")
    long_name = file_name("A", "あ" * 300)
    assert len(long_name.encode()) <= 200 and long_name.startswith("A - あ"), "long names are cut at a whole character"
    assert "/" not in file_name("AC/DC", "../../etc/passwd")
    assert not ok("https://youtu.be/aaaaaaaaaaa\nhttps://youtu.be/bbbbbbbbbbb"), "two links at once are rejected"

    live = rel("Alive 2007", "Album", ["Live"], "1999")
    assert pick_release([rec("One More Time", [live])], "Daft Punk", "One More Time", None, 320) is None, "a live album is not the song's album"
    assert pick_release([rec("One More Time", [live])], "Daft Punk", "One More Time", "Alive 2007", 320)[1] is live
    print("selftest ok")


def main():
    if "--selftest" in sys.argv:
        return selftest()
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
                            handlers=[logging.handlers.RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=1)])
    except OSError:
        logging.basicConfig(level=logging.INFO)
    import tkinter as tk

    root = tk.Tk(className="SongGrab")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
