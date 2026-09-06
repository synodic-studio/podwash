import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from src.pipeline.editor import (
    cut_ads,
    _speech_intervals,
    _resolve_cut_ranges,
    _ad_boundaries,
    _verify_gap_extensions,
)
from src.pipeline.classifier import _merge_ad_segments, _detect_obvious_ad_segments

p = Path("data/classification-eval/journal-new")
s = json.loads((p / "baseline.json").read_text())
r = json.loads(
    Path(
        "data/classification-eval/sentences/journal-current/dsf-current-journal_lee_pace.json"
    ).read_text()
)
ads = _merge_ad_segments(r["parsed"]["ad_segments"], _detect_obvious_ad_segments(s))
speech = _speech_intervals(s)
duration = 1174.2301875


async def run():
    before = _resolve_cut_ranges(ads, speech, duration, 0.5)
    t = time.monotonic()
    after = await _verify_gap_extensions(
        p / "original.mp3", [_ad_boundaries(a, speech) for a in ads], before, duration
    )
    row = dict(before=before, after=after, seconds=time.monotonic() - t, ads=ads)
    (p / "guard-comparison.json").write_text(json.dumps(row, indent=2))
    print(json.dumps(row), flush=True)
    print(
        await cut_ads(
            p / "original.mp3", p / "guarded-clean.mp3", ads, transcript_segments=s
        )
    )


asyncio.run(run())
