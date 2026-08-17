"""Beat 1 of scripts/demo.sh: measure a live proxy feed against its own audio.

Reads the feed XML the demo just fetched, picks the newest completed episode,
and ffprobes the cleaned MP3 the item points at. Everything printed here is
either quoted out of the feed or measured off the network in the last second.
"""

import os
import subprocess
import sys
import xml.etree.ElementTree as ET

ITUNES = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
# The generator marks a finished episode by prefixing its title. See
# src/feeds/generator.py -- an unprocessed episode gets a different prefix.
DONE_PREFIX = "|"


def hhmmss(seconds: float) -> str:
    seconds = int(round(seconds))
    sign = "-" if seconds < 0 else ""
    seconds = abs(seconds)
    return f"{sign}{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    parts = text.split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def ffprobe(url: str) -> dict:
    proc = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration,bit_rate,size",
            "-of", "default=nw=1",
            url,
        ],
        capture_output=True,
        text=True,
        timeout=90,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or "ffprobe failed").strip().splitlines()[-1])
    out = {}
    for line in proc.stdout.strip().splitlines():
        k, _, v = line.partition("=")
        out[k] = v
    return out


def main() -> int:
    feed_xml = os.environ["FEED_XML"]
    have_ffprobe = bool(os.environ.get("HAVE_FFPROBE"))

    root = ET.parse(feed_xml).getroot()
    channel = root.find("channel")
    items = channel.findall("item")
    done = [i for i in items if (i.findtext("title") or "").startswith(DONE_PREFIX)]

    print(f'channel        {channel.findtext("title")}')
    print(f'built          {channel.findtext("lastBuildDate")}')
    print(f"items          {len(items)} published, {len(done)} carrying cleaned audio")

    if not done:
        print("\nNo completed episode in this feed yet -- nothing to measure.")
        return 0

    item = done[0]
    title = (item.findtext("title") or "")[len(DONE_PREFIX):]
    enclosure = item.find("enclosure")
    audio_url = enclosure.get("url") if enclosure is not None else None
    advertised = parse_duration(item.findtext(f"{ITUNES}duration"))

    print(f"\nnewest done    {title}")
    print(f'published      {item.findtext("pubDate")}')
    if advertised:
        print(f"source runtime {hhmmss(advertised)}  (as the publisher's own RSS states it)")

    if not audio_url:
        print("item has no enclosure -- nothing to measure.")
        return 0

    if not have_ffprobe:
        print("\nffprobe not installed, so the cleaned audio goes unmeasured.")
        print(f"\n  {audio_url}")
        return 0

    print(f"\n$ ffprobe {audio_url}")
    try:
        probe = ffprobe(audio_url)
    except Exception as exc:  # noqa: BLE001 -- the demo prints, it does not raise
        print(f"ffprobe failed: {exc}")
        print(f"\n  {audio_url}")
        return 0

    measured = float(probe.get("duration", 0) or 0)
    size = int(probe.get("size", 0) or 0)
    rate = int(probe.get("bit_rate", 0) or 0)
    print(f"clean runtime  {hhmmss(measured)}   {size:,} bytes at {rate // 1000} kbps")
    if advertised and measured:
        print(f"removed        {hhmmss(advertised - measured)}  "
              f"({(advertised - measured) / advertised * 100:.1f}% of the episode)")

    # Full URLs on their own line, so they are clickable in a terminal and
    # pasteable into the call's chat. Invite the audience to check them.
    print()
    print(f"  {channel.findtext('link')}")
    print(f"  {audio_url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
