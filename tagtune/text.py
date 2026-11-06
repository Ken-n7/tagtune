"""URL validation and title/artist text matching."""
import re
from urllib.parse import parse_qs, urlparse

from .config import DURATION_SLACK, MAX_COVER_BYTES, YT_HOSTS

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
          "visualizer", "visualiser", "hd", "uhd", "hq", "4k", "mv", "m", "v", "remaster", "remastered", "full", "with",
          "upgrade", "upgraded"}
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


def safe_name(text, fallback):
    """text made safe as a file or folder name: never hidden, and short enough for any filesystem."""
    from yt_dlp.utils import sanitize_filename

    name = sanitize_filename(text).lstrip(". ") or fallback
    return name.encode()[:200].decode(errors="ignore").rstrip(". ") or fallback


def file_name(artist, title):
    return safe_name(f"{artist} - {title}", "song")


def guess_tags(info):
    """(artist, title, album) from yt-dlp info, falling back to the video title and channel."""
    artist = (info.get("artists") or [None])[0] or info.get("artist")
    title = info.get("track") or info.get("title") or "Unknown"
    channel = CHANNEL_SUFFIX.sub("", info.get("uploader") or info.get("channel") or "") or "Unknown"
    cover = not info.get("track") and COVER.search(title)
    if cover:
        artist = channel  # a cover belongs to whoever performs it, not the artist named in the title
    sep = DASH.search(title)
    left, right = (title[:sep.start()], title[sep.end():]) if sep else (title, "")
    if sep and not cover and norm(right) in {norm(artist), norm(channel)} - {""}:
        artist, title = artist or right, left  # "Song – Artist" video titles
    # "Artist - Song" video titles, and track names that repeat the artist (common on SoundCloud)
    elif sep and (norm(left) == norm(artist) or not (info.get("track") or cover)):
        artist, title = artist or left, right
    return (artist or channel).strip(), clean_title(title), info.get("album")


def norm(s):
    return re.sub(r"\W+", "", (s or "").casefold())


DASH = re.compile(r"\s+[-–—]\s+")  # "Artist - Song", also with en and em dashes
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
