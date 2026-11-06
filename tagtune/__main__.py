"""Entry points: GUI and --selftest."""
import logging.handlers
import sys

from .config import LOG_FILE, MAX_COVER_BYTES
from .text import COVER, clean_title, file_name, guess_tags, image_mime, same_artist, same_song, same_title, title_key, validate_url
from .errors import Problem, friendly
from .metadata import pick_release, pick_track, tags_from
from .gui import App

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

    root = tk.Tk(className="TagTune")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
