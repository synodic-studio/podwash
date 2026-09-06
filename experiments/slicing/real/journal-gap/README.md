# Journal: speech missing from a transcript gap

The existing ad classifier was held fixed. A new download of “My Monday Morning:
Lee Pace” from the user's Journal feed contains conversational speech immediately
after the Indeed ad. Base Whisper reports the ad ending at 150.72 and the next
word at 168.70. A small.en transcription of the same source interval finds the
interviewer's question at 152.22: “Your first credit, at least on IMDb, is Law and
Order SVU,” followed by the interviewee's New York City Yearbook explanation.

The pre-safeguard editor extends the ad cut through that 17.98-second apparent gap.
A saved recording fetched from the production feed also jumps from the show
introduction to “…first credit, right?” without the preceding exchange. Historical
source bytes were not retained, and dynamic ads change downloads, so the served
recording cannot establish identical historical timestamps or the exact historical
cause. The controlled new-source reproduction establishes the gap-deletion
mechanism independently. It does not establish how frequently listeners encounter it.

## Safeguard

Before extending a cut into a transcript gap, the editor decodes only that proposed
extension and checks it with the Silero VAD already bundled in faster-whisper.
Speech detection vetoes the entire extension; VAD timestamps are never used as
word-accurate cut positions. Detector failure likewise preserves the gap. Explicit
ad selection, confidence filtering, model choice and sponsor guardrails are unchanged.
The earlier individual-word-boundary fix remains in place.

On this recording the safeguard keeps the Indeed cut end at 150.72. The rendered
output's independent small.en readback includes the previously missing exchange.
The saved before/after cut lists use the same existing-prompt predictions plus
production sponsor guardrails. Verification of six ads took 1.96 seconds locally;
there are no paid inference calls for VAD. An actual decoded silence control was
classified as non-speech and retained the ability to remove verified silence.

This is deliberately conservative: VAD can include adjacent ad speech or music
in its speech region, causing extra pauses/music to remain. In this Journal test
it vetoed all proposed extensions. It cannot repair wrongly selected ad words,
prove that every unrecognized sound is non-speech, or recover transcript text.
Without any transcript timings the existing padding fallback remains unchanged.

## Evidence and reproduction

The compressed base transcript and source hash are in the classification corpus.
Original, served and guarded MP3s remain under data/classification-eval/journal-new;
audio-hashes.json identifies their exact bytes. The source and served audio are
not interchangeable. JSON readbacks and guard-comparison.json are retained here.
No human listening or phonetic ground-truth annotation was performed.

From the repository root, with the saved source MP3 and scratch classifier record:

```sh
uv run --extra worker python experiments/slicing/real/journal-gap/render-journal-guard.py
uv run --extra worker python experiments/slicing/real/journal-gap/check-guarded.py
uv run --extra worker pytest tests/test_editor.py -q
```

The rendering script writes a local comparison file; it does not replace published
audio or change subscriber GUIDs. Its input baseline is the existing cheap prompt,
not the experimental sentence classifier. check-journal-gap.py repeats the source
readback; check-production.py reads the separately saved served recording. These
scripts use already downloaded models/audio and save only local experiment outputs.

Next validation should inspect cut-adjacent audio from additional subscribed
shows with production selections held fixed, tracking content loss separately
from extra silence and counting actual commercials separately from boilerplate.
