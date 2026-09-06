"""Offline timestamp-overlap audit with fixed word selections, not acoustic truth.

Run from repo root: uv run python experiments/slicing/real/fixed-selection.py
"""

import gzip
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from src.pipeline.editor import _resolve_cut_ranges, _speech_intervals

root = Path("experiments/classification")
manifest = json.loads((root / "manifest.json").read_text())
cases = json.loads((root / "cases.json").read_text())
rows = []
for source, metadata in manifest["transcripts"].items():
    segments = json.loads(gzip.decompress((root / metadata["path"]).read_bytes()))
    words = [w for s in segments for w in s["words"]]
    selections = sorted(
        {
            tuple(r)
            for c in cases
            if c["transcript"] == source
            for r in c["remove_ranges"]
        }
    )
    duration = max(w["end"] for w in words)
    for first, last in selections:
        start, end = words[first]["start"], words[last]["end"]
        prev_end = words[first - 1]["end"] if first else 0
        next_start = words[last + 1]["start"] if last + 1 < len(words) else duration
        # Preserve a small margin beside kept words without retaining long gaps.
        guarded = (
            min(prev_end + 0.08, (prev_end + start) / 2) if first else 0,
            max(next_start - 0.08, (end + next_start) / 2)
            if last + 1 < len(words)
            else duration,
        )
        cuts = {
            "current_exact": _resolve_cut_ranges(
                [dict(start=start, end=end)], _speech_intervals(segments), duration, 0.5
            )[0],
            "current_rounded": _resolve_cut_ranges(
                [dict(start=round(start, 1), end=round(end, 1))],
                _speech_intervals(segments),
                duration,
                0.5,
            )[0],
            "guarded_word_ids": guarded,
        }
        for method, (a, b) in cuts.items():
            affected = []
            for i, w in enumerate(words):
                overlap = max(0, min(b, w["end"]) - max(a, w["start"]))
                if not first <= i <= last and overlap > 1e-7:
                    affected.append(
                        dict(id=i, word=w["word"], overlap_seconds=round(overlap, 6))
                    )
            rows.append(
                dict(
                    source=source,
                    selection=[first, last],
                    selected_edges=[words[first]["word"], words[last]["word"]],
                    method=method,
                    requested=[start, end],
                    cut=[a, b],
                    neighbor_overlap_seconds=round(
                        sum(w["overlap_seconds"] for w in affected), 6
                    ),
                    affected_words=affected,
                )
            )
summary = {}
for method in ["current_exact", "current_rounded", "guarded_word_ids"]:
    records = [r for r in rows if r["method"] == method]
    summary[method] = dict(
        cuts=len(records),
        cuts_overlapping_unselected_words=sum(
            bool(r["affected_words"]) for r in records
        ),
        overlap_seconds=round(sum(r["neighbor_overlap_seconds"] for r in records), 6),
        word_cut_intersections=sum(len(r["affected_words"]) for r in records),
    )
result = dict(
    limitations="Overlap with transcript word intervals, not measured phonetic clipping. Unselected words may be unscored or separately removable. Guarded algorithm uses the same timestamps as the metric; zero overlap is not independent accuracy evidence.",
    summary=summary,
    rows=rows,
)
Path("experiments/slicing/real/fixed-selection-results.json").write_text(
    json.dumps(result, indent=2) + "\n"
)
print(json.dumps(summary, indent=2))
