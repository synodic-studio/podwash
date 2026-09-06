# Audio slicing experiment

Date: 2026-09-06. Research only; production pipeline and dependencies unchanged.

## Finding

A lightweight cutter change is worth testing on real recordings before adopting
another alignment package. stable-ts 2.19.1 did not improve end-to-end cuts in
this controlled experiment. This is not a general accuracy ranking of aligners.

Six synthetic recordings: macOS Samantha at 175 words/minute and Daniel at 210,
each with 0, 100, and 300 ms gaps around the same sponsor passage. Three phrases
were synthesized separately, trimmed to amplitude >100 (16-bit PCM) with 10 ms
margin, and concatenated. Their sample offsets provide reference passage bounds.
These are construction bounds, not manually annotated phonetic boundaries.

Each recording used faster-whisper base/int8, stable-ts with the same base/int8
model, and stable-ts refinement. Both exact construction timestamps and known
word selection were fed into the current editor. Classification was deliberately
excluded. The alternative prototype cuts halfway between the last kept word's
end and first removed word's start, and similarly at the other end.

Average milliseconds per removed passage, summed across both boundaries:

a. Current editor + faster-whisper: 147.5 content clipped, 0 ad retained.
b. Current editor + stable-ts: 240.8 content clipped, 0 ad retained.
c. Current editor + stable-ts refinement: 228.3 content clipped, 0 ad retained.
d. Prototype + faster-whisper: 14.0 content clipped, 47.3 ad retained.
e. Prototype + stable-ts: 33.7 content clipped, 47.3 ad retained.
f. Prototype + refinement: 27.3 content clipped, 63.2 ad retained.

The prototype reduced clipping by about 91% with baseline timestamps, but trades
some over-cutting for under-cutting. Retained milliseconds can include a trailing
phoneme, quiet audio, or our construction margin. This metric does not establish
perceived quality; no listening evaluation was performed.

## Deterministic editor issue

Even perfect timestamps can fail: words [0,1], [1,1.2], [1.2,1.8],
[1.8,2], [2,3] merge into one speech interval [0,3]. Requesting removal
of [1,2] produces [0.5,2.5], clipping 500 ms on each side. Touching word
intervals lose their internal edges in `_speech_intervals`, then the mid-speech
fallback pads the cut. The existing 25 editor tests all pass despite this case.

The prototype preserves word identity, but currently assumes the first and last
removed words are already known. Production's classifier receives segment-level
timestamps; a practical implementation needs word selection or a local boundary
classification step too. Better alignment cannot decide which words are ads.

## Reproduce

Run from the repository root. Requires macOS built-in Samantha/Daniel voices,
ffmpeg, uv, and model download access on the first run. The isolated environment
was about 908 MB; model cache is additional. This is a resolved experimental
snapshot, not a proposed runtime dependency set.

```sh
uv venv /tmp/podwash-slicing-venv --python 3.12
uv pip install --python /tmp/podwash-slicing-venv/bin/python -r experiments/slicing/requirements.txt
/tmp/podwash-slicing-venv/bin/python experiments/slicing/run.py
/tmp/podwash-slicing-venv/bin/python experiments/slicing/compare.py
VOICE=Daniel RATE=210 EXPERIMENT_DIR=data/slicing-experiment/daniel /tmp/podwash-slicing-venv/bin/python experiments/slicing/run.py
EXPERIMENT_DIR=data/slicing-experiment/daniel /tmp/podwash-slicing-venv/bin/python experiments/slicing/compare.py
```

`results.json` preserves combined measurements. Generated source WAVs,
transcripts, raw results and current/prototype output WAVs live under
`data/slicing-experiment/` (gitignored). `refine` timings measure the additional
refinement pass only. Tests were run with `uv run pytest tests/test_editor.py -q`.

## Next experiment

Capture original bytes, transcript and classifier decisions together for real
episodes before cleanup. Dynamic ad insertion means re-downloading an old URL
is not a valid reference for previously processed audio. Annotate boundary words
on those exact bytes and compare the prototype, baseline and a separate forced
aligner such as WhisperX. Include rapid transitions, music beds, overlapping
speech and sponsor reads within a transcript segment. No known-bad original
recordings were present in this checkout, so this experiment does not establish
the cause or fix of a particular listener-reported cut.

References: https://github.com/jianfch/stable-ts and
https://github.com/m-bain/whisperX . WhisperX was not installed or benchmarked.

## Real podcast follow-up

See [real/README.md](real/README.md) for the two-podcast comparison, reproduced
word losses, WhisperX results, and classifier experiments.
