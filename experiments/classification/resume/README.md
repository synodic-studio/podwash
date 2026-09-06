# Resumed cheap-classifier experiments

Run scripts from the repository root with a Python environment containing requests.
They call the local proxy's dsf alias, bypass response caching, and overwrite their
scratch outputs under data/classification-eval/resume. They are exploratory snapshots,
not production commands. Run windows.py before refine.py; reasoning.py is independent.
The transcript and annotations come from the checksummed parent eval suite.

The full Ologies transcript was divided into 500-word cores with 100 words of context
on either side. Each word's decision belongs to its core; no gold boundaries selected
the windows. Fifteen calls cost $0.02485 in proxy-reported inference charges. They
missed 31 of 551 labeled removable words and selected 252 of 1,257 labeled editorial
words. No calls failed. The remaining transcript words are not fully annotated.

A second pass reviewed each proposed cut with 100 neighboring words on each side,
allowing rejection, splitting, and expansion. Total two-stage cost was $0.03030.
It missed 126 removable words and selected 124 editorial words. This prompt was
adapted after inspecting window failures, so this is development evidence, not a
holdout result. Neither approach is suitable for production. These costs exclude
transcription, audio processing, and hosting.

The review reduced over-removal of charitable discussion, but still removed the
opening editorial framing and fragmented the sponsor anecdote. More local context
and a second call did not reliably solve the semantic distinction. Disagreement at
window seams also fragmented a self-promotion passage.

Raw window/review outputs are alongside these scripts; aggregate records are in
../runs/window-review.json and can be rescored offline. Reasoning-mode records are
in ../runs/dsf-low-reasoning.json. A length error is a failed inference, not an empty
ad prediction. No production configuration was changed.

Next action: validate the existing word-boundary cutter prototype independently of
classification before investing further in prompt tuning. Speaker changes remain an
unmeasured auxiliary cue; they cannot fix same-speaker editorial/ad distinctions.

Enabling low reasoning on dsf across ten saved excerpts cost $0.14401.
Five calls exhausted the 16,000-token output budget. Of five completed calls,
four lost no labeled editorial words, while the other removed the show identity.
This did not establish a reliable improvement and is not recommended as a default.
