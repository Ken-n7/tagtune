"""User-facing errors."""
import re


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
        ("permission denied", "Can't write to the save folder. Check its permissions or pick another one."),
        ("read-only file system", "Can't write to the save folder. Check its permissions or pick another one."),
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
