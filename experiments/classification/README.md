# Podcast classification evals

A small, offline-first corpus for testing cheaper classifiers and prompts.
**The default model for an explicitly requested live run is `dsf`.** GPT and
Opus results are retained as comparison baselines, not production recommendations.
The production classifier is unchanged; cutter fixes are documented separately.

## Saved material

a. Five compressed transcript files from three real podcasts: a complete Ologies
episode, opening/closing excerpts of The Daily, and opening/closing excerpts of
The Ezra Klein Show. These files are self-contained and do not depend on `/tmp`,
a model installation, API access, or another download. Total suite size is small.
`manifest.json` records titles, source feeds, original-audio hashes, transcript
hashes, crop offsets, transcriber settings, and the model alias mappings used.

b. Eleven labeled cases: five development excerpts, two initially held-out Ezra
excerpts, three additional Ologies cases, and one full-context Ologies case. Cases
cover sponsor acknowledgments, host reads, Patreon/review promotion, staff credits,
news after a break, genuine show introductions, and editorial company mentions.

c. Three frozen prompt versions and 78 recorded responses, including failures,
usage, proxy-reported costs, model identifiers, and uncached repeat measurements.
Ten GLM calls were unavailable because the provider reported insufficient balance;
they are errors, never counted as successful classifications.

Reference labels were assigned by Codex reviewing the transcripts. They reflect
podwash's current editorial policy, not human-adjudicated ground truth. Ambiguous
recap/promotional material is explicitly unscored. The full-context case scores
551 removable and 1,257 preserved words; another 5,496 words are unscored.
The additional three cases supply part of that full-context reference set.

## No-cost commands

Run from the repository root with system Python; only the standard library is
needed. Neither command below contacts an API or loads an audio model.

```sh
python3 experiments/classification/evaluate.py check
python3 experiments/classification/evaluate.py score
python3 experiments/classification/evaluate.py score --json
```

`check` verifies transcript hashes, word IDs, and disjoint reference labels.
`score` recomputes the saved predictions against current reference labels.
Model/API errors are displayed separately. Overlapping predictions are counted
once, invalid word IDs are rejected, and intentionally unscored words do not
become false positives or false negatives.

Scoring reports **content words lost** and **removable words missed** separately.
Removable means ads, credits, promotion and break transitions. Timestamp responses
are mapped by word midpoint at the production 0.7 confidence threshold; word-ID
responses use exact inclusive indices. This measures classification before the
production guardrails/editor. It does not measure acoustic cut accuracy.

## Explicit live comparisons

The `run` subcommand is the only network operation. It defaults to DeepSeek via
the existing local proxy, with five development cases. It never chooses a more
expensive fallback. GLM/GPT/Opus or another alias require an explicit model name.

```sh
# Inspect the planned input without calling the API.
python3 experiments/classification/evaluate.py run --dry-run --case ologies_opening

# One cheap comparison, saving the response and its reported cost.
python3 experiments/classification/evaluate.py run --model dsf --prompt words-v2 --case ologies_opening

# Re-score a saved run directory without more inference.
python3 experiments/classification/evaluate.py score --results data/classification-eval/runs/<run-directory>
```

`--case` may be repeated. The full episode is only run when explicitly selected
with `--case ologies_full`. `--api-base` selects another OpenAI-compatible endpoint;
`PODWASH_EVAL_API_KEY` supplies optional authorization without writing it into
results. `--output` chooses the result directory; existing records are never
overwritten. Default output is under gitignored `data/classification-eval/runs/`.

Live runs disable LiteLLM response caching and storage so repeated results are
fresh inference. Provider prompt-token caching may still reduce costs normally.
An exploratory repeat that was served from the response cache is excluded from
the saved eval runs; it was not evidence of model stability. Historical cost
headers are approximate provider/proxy accounting, not reconciled invoices.

New word-ID prompt templates can be added under `prompts/` and selected by name.
Their JSON contract is `{"ranges":[{"first":9,"last":14}]}`. The reserved
`current` prompt uses the existing timestamp contract. Prompt hashes are recorded
for new runs. Preserve old versions when experimenting so results remain traceable.

## What these runs established

a. On the five initial excerpts, current DeepSeek lost 212 of 1,570 labeled
content words and missed 7 of 208 removable words. The revised word-ID prompt v2
reduced content loss to 82 and missed 11. It is not ready to replace production.
The five DeepSeek v2 requests cost about $0.0053 according to the proxy.

b. A first GPT word-ID run looked almost perfect on those excerpts, but an
uncached repeat lost seven content words. On the full episode, GPT with v2 lost
51 of the 1,257 scored content words and missed 118 of 551 removable words. That
request cost about $0.229. A larger model did not solve the problem reliably.

c. On the same full-context case, current DeepSeek lost 91 scored content words
and missed 194 removable words; DeepSeek v2 lost 147 and missed 188. Long-context
behavior differs materially from excerpt behavior. Do not select a model based
only on an attractive excerpt score.

d. The original holdout was unseen by the v1 prompt. Its results informed later
analysis, so subsequent `holdout-v2-regression` results are regression checks,
not a fresh blind holdout. Avoid presenting these small, correlated samples as a
general benchmark or training a prompt until it merely memorizes them.

## Speaker-change hypothesis and next inexpensive experiments

Diarization is a candidate **boundary cue**, not an ad label. A different speaker
can mark an inserted commercial, but a guest answering a question also changes
speakers. Host-read ads may have no speaker change at all. Speaker identities or
speaker-change timestamps have not been measured in this corpus yet.

Test these comparisons without changing the reference transcript labels:

a. Same-speaker host-read ads: does content-based detection still find both ends?
b. Different-speaker inserted ads: does a speaker change tighten a likely boundary?
c. Editorial interviewer/guest changes: do they avoid false ad detections?
d. Music/pauses beside both ads and editorial transitions: does combining cues
help without making either cue sufficient by itself?

Next: use a cheap classifier on overlapping local transcript windows, then refine
only the proposed boundary words with word IDs and optional speaker/pause/music
cues. Compare against the saved full-context and excerpt results. Keep false cuts
of editorial content visible rather than hiding them inside a single accuracy score.
The expensive models are useful recorded references; they are not the intended
long-term runtime.

## Maintenance

`cases.json` contains compact inclusive word-ID ranges; IDs count words from zero
within their source transcript. `include_ranges` chooses the input, `remove_ranges`
and `keep_ranges` specify scored expectations, and all other included words are
unscored. Keep annotations fixed while comparing model/prompt variants.

Original MP3s, edited listening samples, raw transcripts and exploratory scripts
remain in `data/slicing-real/` and `data/classification-eval/`. Their audio hashes
matter because dynamic ad insertion can change a later download of the same URL.
The compressed transcripts and this scoring suite remain usable independently.

Tests: `uv run pytest tests/test_classification_eval.py -q`.

## Resumed cheap experiments

See [window and review results](resume/README.md): full-episode overlapping windows
cost about 2.5 cents; adding a review pass brought the total to about 3 cents.
Both still selected substantial editorial content. The saved failures are useful
regressions, not a production recommendation.

## Sentence experiments and production baseline

[Sentence/clause results](sentences/README.md) include a fresh Journal comparison
and an unused Daily holdout. They do not establish improvement over the user's
reported low audible-ad rate. Model-only scores exclude production sponsor
guardrails and count credits/promos/transitions alongside ads. A separate local
VAD cutter safeguard addresses reproduced loss of untranscribed speech.
