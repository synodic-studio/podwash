# Real podcast slicing experiment

2026-09-06. Two downloaded podcasts, 34 rendered comparison clips. Original
bytes are preserved with SHA-256 checks in `data/slicing-real/`; titles and
hashes are recorded in `results-summary.json`. Production is unchanged.

## Recommendation

Keep faster-whisper and fix the small cutter layer first. stable-ts did not
resolve the reproduced clipping. WhisperX helps some boundaries, but exchanging
aligners without changing the cutter can introduce clipping elsewhere. Consider
WhisperX as an optional local refinement stage, not a mandatory replacement.

## Real recordings

a. Ologies: “Party Psychology (WHY WE CELEBRATE) with Danielle Brick & Kelley
Gullo-Wight,” from https://feeds.simplecast.com/FO6kxYGj . Downloaded the complete
44-minute episode and transcribed it with the production base/int8 settings.
Compared the opening sponsor mention, sponsor read, and a short inline sponsor
phrase using base, stable-ts, stable-ts refinement, and WhisperX alignment.

b. The Daily: “A.I. Is Outsmarting Its Creators,” from
https://feeds.simplecast.com/54nAGcIl . Downloaded the complete episode and
transcribed its first 240 and last 210 seconds. Compared a network credit and
a music-backed break transition. Ran WhisperX on the break's surrounding speech.

## What happened

a. **Actual neighboring-word loss reproduced.** In Ologies, selecting the words
“by booking a verbo” gives base timestamps 36.62–37.88. The current editor cuts
36.12–38.38 instead, because the requested boundaries sit inside merged speech
intervals. Automated readback loses the preceding word “myself.” The prototype
cuts 36.62–37.88 and readback preserves it. stable-ts and its refinement pass
still lose “myself” with the current editor. WhisperX preserves it in this case.
This is a deliberate word-selection precision test on real speech, not a claim
that this short phrase alone is the correct complete ad-removal policy.

b. **An aligner swap can make another cut worse.** At the opening sponsor mention,
WhisperX places the ad's last word at 4.542 and the following “I've” at
4.622–4.763. The current 300 ms tolerance skips that entire next word and resumes
at 4.823. Readback loses “I've.” The prototype resumes at 4.582 and preserves it.
Base timestamps with the current editor already pass this example's word check.

c. **Large gaps need a different policy from short gaps.** On The Daily, midpoint
cuts preserve about five extra seconds around a break, relative to the current
cutter. A second small prototype retains at most an 80 ms guard beside each kept
word while removing the rest of the gap. With fixed source context and VAD-enabled
readback, current, midpoint and guarded cuts all retain “Cheers, Michael” and the
start of the following news bulletin. Earlier readbacks with varying context and
without VAD inconsistently omitted speech; they are retained as raw exploratory
results, not treated as evidence that a word was actually deleted.

WhisperX's unpadded alignment could not move “Michael” beyond the existing
segment end of 60.660. Expanding alignment windows by 500 ms gave 60.816 instead.
That demonstrates sensitivity to the supplied segment bounds, not independently
measured ground-truth accuracy.

d. **Classification remains a separate problem.** Running the current prompt and
configured `dsf` backend on the complete Ologies transcript returned removals at
0.6–4.2 and 28.0–64.1. The editor resolves those to 0–4.6 and 27.5–64.82. The latter
starts before the personal sponsor anecdote around 34.5; the closing Vrbo pitch
around 2661–2673 was missed. A 90-second excerpt caused much worse over-removal,
so that excerpt result should not represent full-episode behavior.

Two experimental word-ID prompts caught closing sponsor material but respectively
fragmented the ad and over-expanded the opening passage. Word IDs solve boundary
addressing; they do not automatically solve editorial judgment. Do not deploy
those prompt variants as fixes.

## Evidence and limitations

Edited audio was independently transcribed with faster-whisper small.en, versus
base for the working transcript. No manual listening or phonetic ground-truth
annotation was performed, so there is no claimed real-audio millisecond accuracy
score or general package ranking. Readback can reconstruct partially clipped words
or omit intact speech. The strongest cases above combine source word intervals,
actual rendered cut positions, and consistent readback differences.

Useful local artifacts:

a. `data/slicing-real/inline-sponsor-baseline-current.wav` and
`inline-sponsor-baseline-prototype.wav`: “myself” comparison.
b. `data/slicing-real/short-sponsor-whisperx-current.wav` and
`short-sponsor-whisperx-prototype.wav`: “I've” comparison.
c. `data/slicing-real/daily/break-baseline-guarded.wav`: guard plus gap removal.
d. `data/slicing-real/*evaluation.json` and
`data/slicing-real/daily/*evaluation.json`: readbacks and exact cut ranges.
e. `data/slicing-real/full-classification.json`: full-context model response.

## Running the experiments

Run scripts from the repository root. They use the preserved recordings in
`data/slicing-real/`, not production storage. The original bytes are necessary
for historical comparisons; a fresh download may contain different dynamic ads.
The `download-daily.py` helper fetches a new feed item and overwrites its local
experimental original, so do not run it when reproducing this saved result.

Use `/tmp/podwash-slicing-venv/bin/python` for transcription, stable-ts, and
readback scripts; its dependency snapshot is `../requirements.txt`. Use
`/tmp/podwash-whisperx-venv/bin/python` for `align*.py`; its resolved dependencies
are `whisperx-requirements.txt`. WhisperX 3.8.6 used its default English
wav2vec2 model on CPU. Both environments and model caches remain available.

1. Run `transcribe.py` for the complete Ologies transcript.
2. Run `boundary.py`, then `align.py` for opening transcription/alignment variants.
3. Run `evaluate-opening.py` and `evaluate-inline.py` to render and read back cuts.
4. Run `daily-tail.py`, `align-daily.py`, then `align-daily-padded.py` for Daily data.
5. Run `evaluate-daily.py` and `daily-refined.py` for the Daily comparisons.

`classify*.py` scripts call the existing local model proxy at localhost:4000,
model `dsf`; they can incur model charges. Full-episode classification used
23,414 input and 189 output tokens. They do not alter live feeds or jobs.
An initial excerpt request exhausted its output budget in reasoning; the saved
successful scripts include the existing backend's extra-body thinking setting.

## Next implementation

Extract a deterministic word-range cutter with explicit selected word IDs,
protected neighboring words, a small guard at long gaps, and optional boundary
alignment. Regression fixtures should include both real word-loss cases above
and the synthetic touching-word case. Keep classification and its evaluation
separate. This experiment supports that implementation, not a whole-pipeline
migration to stable-ts or WhisperX.

## Fixed-selection follow-up

`uv run python experiments/slicing/real/fixed-selection.py` audits all 12 distinct
removable word ranges in the saved classification corpus, holding word selection
fixed. With exact first/last-word timestamps, the current cutter overlaps seven
unselected word intervals across two cuts, totaling 1.00 seconds. Rounding the
same requested times to one decimal changes the total to 1.02 seconds, with the
same two affected cuts. The guarded word-ID prototype overlaps none. These are
transcript-interval measurements, not phonetic ground truth; the prototype uses
the same timestamps being measured, so its zero is not independent validation.

Both affected cuts are in Ologies. Removing words 97–178 starts the current cut
at 33.52 instead of 34.02, overlapping “talk to them.” Removing credits 6807–6939
ends it at 2536.40 instead of 2535.90, overlapping “And if you stick.” This holds
selection constant and isolates a cutter error from semantic classification.

`fixed-readback.py` renders both cases with identical seven-second source context
on each side, then transcribes with small.en and VAD. Current output readback
omits “talk to them” and “And if you”; guarded output retains both. Four WAVs are
saved under data/slicing-real/fixed-*.wav. The transcript, exact cuts, and context
are recorded in fixed-selection-readback.json. No manual listening was performed;
ASR can reconstruct partial words (as with “stick”) or omit intact speech.

Next action: implement and regression-test a cutter path that preserves individual
word boundaries. The prototype assumes explicit selected word IDs; production
currently supplies approximate timestamps, whose mapping must be tested separately.

## Production cutter implementation

The editor now retains individual word spans, snaps to the nearest corresponding
start/end within 50 ms (plus floating-point tolerance), and leaves genuinely
mid-speech boundaries unpadded. Existing gap absorption and no-transcript padding
remain. This implements the clipping fix without adding the prototype's 80 ms
margin or changing the classifier's timestamp contract.

`fixed-selection-after-fix.json` records zero neighboring-word interval overlaps
for all 12 saved cuts, for both exact and rounded requests. The previous results
remain in fixed-selection-results.json. Rerendered production cuts and automated
small.en readbacks in fixed-selection-after-fix-readback.json preserve “talk to
them” and “And if you.” These remain timestamp/ASR checks, not human phonetic
validation. Incorrect classifier selection and inaccurate STT timestamps can still
produce incorrect cuts.

Regression tests cover touching words, rounding offsets, the short WhisperX
neighbor, and imprecise boundaries inside speech. The full suite passed 209 tests
on rerun; the initial run had an intermittent scheduler failure-counter test
failure with concurrent background polling visible in captured output. That test
also passed in isolation. The cutter fix does not change scheduling.
