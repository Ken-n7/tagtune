"""Tag lookup (MusicBrainz, iTunes, Deezer) and tag writing."""
import json
import re
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import DURATION_SLACK, HTTP_TIMEOUT, MAX_COVER_BYTES, USER_AGENT
from .text import image_mime, norm, same_artist, title_key

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
