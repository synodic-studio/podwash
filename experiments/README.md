# Audio slicing investigation: closeout and index

The broad investigation below is closed at the user's request. A separately requested
[September 16 Journal incident check](journal-2026-09-16/README.md) records actual
surviving commercials and the limits of historical diagnosis. There is no scheduled
follow-up or standing request to continue experimenting. This file is the entry
point; individual reports retain their historical measurements and limitations.

## User baseline and scope

Before this work, the user reported roughly one audible ad per several dozen
episodes; the usual defect was a cut off by a few words. The goal was inexpensive,
precise slicing and determining whether an existing package could replace custom
code. Later classifier research broadened the scope too far. It did not establish
a better real-world ad-miss rate. Prioritize The Daily, The Journal and Ologies if
this work is explicitly resumed; use other shows later for generalization.

## What is deployed

a. Individual word intervals are retained instead of merging touching intervals.
Merging [0,1], [1,2], [2,3] into [0,3] made a correct cut [1,2] appear mid-speech;
the old half-second fallback expanded it to [0.5,2.5]. The cutter now snaps only
within 50 ms of a corresponding word edge and does not pad into known speech.
Tests and controlled Ologies render/readback comparisons support this fix.

b. Proposed extensions into transcript gaps are checked with bundled local Silero
voice activity detection. Transcription was already local; this is an additional
speech-presence check, not another transcription service, speaker diarization or
ad classifier. Speech or detector failure vetoes the entire extension. A Journal
transcript omitted 17.98 seconds between an ad and the next reported word; the
safeguard preserved the conversation found there by independent ASR readback.

c. The existing cheap classifier, production prompt and deterministic sponsor
checks remain unchanged. Neither experimental classifier prompts nor alternative
aligners were deployed. Existing published audio was not replaced or reannounced.
Both cutter changes were loaded by restarting the idle worker and verifying startup.

Relevant commits: 0ce6732 (word boundaries and initial experiments), bc6c201
(gap safeguard and additional experiments), d766124 (failed-run records).
The full suite passed 213 tests after the safeguard; targeted editor/eval checks
passed 43. Earlier full-suite runs exposed an intermittent scheduler-counter test
with concurrent polling; it passed in isolation and on rerun, unrelated to slicing.

## Evidence and limits

[Synthetic benchmark](slicing/README.md): two synthesized voices, three gap lengths,
base Whisper versus stable-ts and refinement. Construction offsets supplied
reference boundaries. It favors repairing the cutter, not a general aligner ranking.

[Real podcast comparisons](slicing/real/README.md): Ologies and The Daily, 34 initial
rendered comparisons, plus fixed-selection follow-ups. stable-ts did not remove the
reproduced cutter defect. WhisperX helped some boundaries but could expose the old
300 ms tolerance skipping a short neighboring word. Padding alignment input windows
changed outputs; it did not establish phonetic accuracy. Midpoint cuts retained
long pauses; an 80 ms guarded prototype reduced that, but was not deployed.

[Fixed selections](slicing/real/fixed-selection-results.json): 12 labeled cuts,
two with neighboring timestamp overlap before the fix and none afterward, including
rounded inputs. Zero overlap against the same timestamps is not independent
acoustic validation. [Readbacks](slicing/real/fixed-selection-after-fix-readback.json)
provide additional evidence, but ASR can reconstruct clipped words or omit intact ones.

[Journal gap report](slicing/real/journal-gap/README.md): source, served and guarded
audio hashes; same-source reproduction; exact cuts; independent readbacks. The
served file lacks the corresponding exchange, but its historical source bytes
were not retained. Dynamic ad insertion prevents claiming identical historical
timings or proving the exact historical cause from a later download alone.

The Silero deployment had narrower validation than earlier rollout language
suggested. It vetoed every proposed extension in the Journal test, not only the
harmful one. Adjacent speech/padding or music can therefore preserve unwanted gaps.
It can also miss speech. The original word-boundary fix has clearer support; the
VAD safeguard is conservative protection, not proof of generally precise cutting.
No human listening or phonetic ground-truth annotation was performed.

## Classification experiments

[Corpus and offline scoring](classification/README.md) stores checksummed compressed
transcripts, reference labels, source hashes, frozen prompts and recorded outputs.
There are now 13 cases. Labels are transcript-review judgments, not listener gold.
Model-only scores omit production deterministic guardrails and count credits,
promotions and break handoffs alongside ads. Some full-transcript words are unscored.

[Window/review experiments](classification/resume/README.md): cheap overlapping word
windows improved recall but overselected editorial content. A second review pass
traded fewer false cuts for more misses. Low-reasoning DeepSeek frequently exhausted
its output budget. These variants were not promoted.

[Sentence/clause experiments](classification/sentences/README.md): sentence windows
with local clause checks preserved all labeled editorial words in two Ologies runs,
but missed 41 versus 105 removable words. Journal repeats preserved content and
removed all labeled commercials, leaving break handoffs. The existing prompt also
removed every labeled Journal commercial; the apparent gain was mostly credits.
Word-ID and exact-quote boundary reviews failed to improve the mixed sentence
reliably. Whole-episode clause classification lost recall. Raw decisions, failures,
prompt hashes, usage and costs are retained, including unsuccessful approaches.

Early GPT/Opus results remain historical references; the user rejected their cost
for long-term use. Subsequent experiments used only cheap dsf, plus local audio
models. GLM was unavailable due provider balance. One early response-cache repeat
was excluded; one initial failed sentence response was not saved and its precise
output/cost cannot be recovered. These limitations are documented rather than
filled in. The fresh Daily West Bank case remains unused by the classifiers.

## Parked ideas, not a work queue

a. Compare the VAD veto with a simpler cap on gap expansion, measuring both lost
speech and retained silence/music with identical production ad selections.
b. Test speaker-change timestamps as an auxiliary boundary cue. Different speakers
can mark inserted ads or normal interviews; host-read ads may have no change.
No diarization experiment or speaker-label corpus was completed.
c. Consider forced alignment only near suspect boundaries, or selective local
retranscription of apparent gaps. Better word timing cannot repair wrong selection
or missing words by itself. A larger transcription model is not yet justified as
the default by these tests.
d. Freeze candidate settings before more held-out tests, repeat inference, and
report actual commercial misses separately from boilerplate and content loss.
e. A small standalone cutter remains plausible, but was not extracted or published.
Stable-ts and WhisperX are alignment components, not demonstrated drop-in solutions
for this entire task. Papercut was an initial research lead, not benchmarked or
validated as a replacement. No existing package was established as the clear winner.
f. If measuring production recurrence, retain exact source identity/hash, transcript,
selected ranges and resolved cuts for sampled jobs; the current worker cleans up
intermediates. Such retention was not added to production.

## Artifact locations and commands

Tracked reports, scripts, compressed corpus and results live under experiments/.
Original audio, rendered comparisons and exploratory intermediates remain in
`data/slicing-experiment/`, `data/slicing-real/` and `data/classification-eval/`
(gitignored). Planet Money and Stuff You Should Know exploratory downloads were
set aside in the latter directory and excluded from the saved evaluation corpus.
Partial/superseded downloads are not completed benchmark results. Experimental
Python environments remain `/tmp/podwash-slicing-venv` and
`/tmp/podwash-whisperx-venv`; dependency snapshots are saved in slicing/.

Offline checks, with no paid inference:

```sh
python3 experiments/classification/evaluate.py check
python3 experiments/classification/evaluate.py score
uv run --extra worker pytest tests/test_editor.py tests/test_classification_eval.py -q
```

Only explicitly invoked live experiment runners contact the model proxy. Individual
reports document reproduction commands and scratch paths. Do not rerun historical
scripts blindly: some overwrite scratch outputs, and later downloads can have
different ads. No further action is required unless the user reopens the task.
