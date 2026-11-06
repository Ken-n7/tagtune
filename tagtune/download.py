"""External tools and the download job."""
import glob
import logging.handlers
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections import Counter
from itertools import chain
from pathlib import Path
from urllib.parse import urlparse

from .config import DURATION_SLACK, MIN_FREE_BYTES, OFFICIAL_SOURCES, PLAYLIST_CONFIRM, _job_dirs, log
from .text import COVER, file_name, safe_name, guess_tags, same_artist, same_song, same_title
from .errors import Problem, friendly
from .metadata import TAG_SOURCES, write_tags

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


def preflight(out_dir):
    """(fatal_message or None, [warnings]) checked once at startup."""
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    if not (out_dir.is_dir() and os.access(out_dir, os.W_OK)):
        return f"Can't save songs: {out_dir} is missing or not writable.", []
    for stale in out_dir.glob(".tmp*"):  # a live job keeps touching its folder, a crashed one doesn't
        if time.time() - stale.stat().st_mtime > 24 * 3600:
            shutil.rmtree(stale, ignore_errors=True)
    if not find_ffmpeg():
        return "ffmpeg wasn't found, so songs can't be converted. Install it with: sudo apt install ffmpeg", []
    warnings = []
    if not (find_node() or shutil.which("deno")):
        warnings.append("Node.js wasn't found, so some YouTube downloads may fail.")
    if shutil.disk_usage(out_dir).free < MIN_FREE_BYTES:
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


def run_job(url, out_dir, emit, cancel, confirm, exact=False):
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
    dest = out_dir  # a playlist's songs go in a folder named after it
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
            final = dest / (file_name(artist, title) + ".mp3")
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
        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=".tmp-", dir=out_dir)
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
                dest = out_dir / safe_name(first.get("title") or "", "Playlist")
                dest.mkdir(exist_ok=True)
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
