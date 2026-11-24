# Recent Journal ad leakage: September 16, 2026

The user reported multiple surviving ads after the earlier slicing investigation.
This is a new production incident check, not a continuation of the broad classifier
benchmark. No production behavior was changed and no paid model calls were made.

## Confirmed finding

The actual served recording of **Are Budget Airlines Facing Extinction?**
(episode 8226112, September 15) contains the beginning of a bilingual Gatorade
Zero Powders advertisement at approximately **455.8–469.2 seconds (7:36–7:49)**.
The pitch describes low energy/concentration and dehydration, then introduces the
product. These are ASR-derived approximate boundaries, not phonetic annotation.
It is a substantial partial commercial, not merely credits or a stray word.

The saved classification identifies Gatorade/Kroger and selects source seconds
554.9–600.2 for removal. Source and served clocks differ after cuts. The historical
source, transcript and resolved cut list were deleted; therefore these records
cannot establish the precise historical onset or whether the classifier chose
incorrect words versus receiving incomplete/wrongly timed transcription.

On the **same served 440–490 second clip**, cached multilingual Whisper base with
Spanish forced recognizes the Gatorade pitch. With English forced, it omits the
pitch and resumes editorial speech. This demonstrates a transcription visibility
failure on these exact bytes. It does not prove that the historical production
transcription took the same path. Production auto-detects language and uses default
context conditioning; this audit's full served readbacks disable previous-text
conditioning and enable VAD, and the verification clip has less context.

A fresh original was downloaded for comparison. Its corresponding break contains
an Instagram Teen Accounts ad instead of Gatorade. Dynamic insertion changed the
recording; the fresh source is explicitly **not** historical ground truth.

## Other confirmed served commercials

a. **My Monday Morning: Twiggy** (8226104, September 13): about 960.9–970.1
seconds (16:01–16:10), through the end of the served file. A short local English
readback independently recovers Eva Longoria introducing a Lenovo pitch. The saved
classification selects source1228.0–1278.5 as Lenovo/Blinds.com post-roll. The
remaining audio is the beginning of the pitch, cut off mid-sentence.

b. **What Happened to the Affordable Car?** (8226097, September 10): a Verizon/NFL
Sunday Ticket promotion remains near the end, beginning around977.3 seconds. See
the saved tail readback for approximate word timings. The stored selection marks
only1108.1–1116.1 source seconds for Verizon; exact historical mapping is unavailable.

These establish multiple actual partial commercials, not merely leftover credits.

## Relationship to the earlier cutter change

The Silero safeguard only vetoes expansion beyond selected/snapped ad intervals.
It cannot undo removal inside an explicitly selected interval. It can preserve
speech omitted from transcription, including both editorial speech and ad speech.
Thus it may expose ad speech that the old gap expansion happened to remove. This
is a plausible mechanism here, not a demonstrated attribution: the historical
transcript and exact executed cuts are unavailable. Rolling back the safeguard
would also restore the known risk of deleting omitted editorial speech.

No classifier/editor change appears between the September 6 investigation and
this report; the intervening tracked change adds per-feed on-demand scheduling.
This does not establish when the actual ad leakage began or a regression rate.

## Evidence retention and limitations

`records.json` retains public episode identifiers, timestamps, exact served hashes,
durations and stored classification records without audio URLs or credentials.
Full served readbacks and recordings are under gitignored
`data/journal-investigation-2026-09-16/<episode-id>/`. Private feed/database
snapshots are in the parent directory. The case verification directory preserves
cropped audio and model responses. Experimental scripts are retained here;
environment versions and evidence JSON remain local with the run records and
are not bundled with these scripts. Reproduction requires those local snapshots;
cached models may need downloading on another machine.

The stored classifier JSON is neither a pristine model response nor the exact
executed cut list. The guardrail merge mutates overlapping model dictionaries,
but guardrail-only additions are absent from serialized model results and
below-threshold model predictions may remain. Do not reconstruct exact edits from
this JSON alone. The server processing-log endpoint returned empty lists for the
examined worker jobs; the worker discards stage logs and deletes intermediates.

Readbacks can miss speech or reconstruct clipped words. A readback with no ad found
is not proof of an ad-free recording, especially given the bilingual counterexample.
No human listening, broad recurrence-rate measurement, new classifier evaluation,
or production reprocessing was performed. Existing episode GUIDs and audio were
left in place.

## Next bounded action

Retain the original transcript, effective selected intervals and resolved cuts for
sampled failing jobs before changing detection. Then test targeted local
retranscription around suspect ad boundaries, including language switches, against
both this retained-commercial case and the earlier Lee Pace editorial-preservation
case. A higher-cost classifier cannot recover words absent from its input. Neither
a blanket language setting nor disabling speech protection is justified by these
confirmed examples. This is a proposed next step, not a scheduled task.
