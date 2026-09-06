# Sentence and clause classification experiments

These are cheap DeepSeek (`dsf`) experiments, not the production classifier.
The user's listening baseline is already good: approximately one audible ad in
several dozen episodes, with occasional cuts off by a few words. These tests do
not establish an improvement in that real-world ad-miss rate. Counts below include
credits, social promotion, and break handoffs as well as commercials.

## Findings

On the full Ologies development case, sentence classification followed by local
clause checks at mixed sentence edges selected zero of 1,257 labeled editorial
words. It missed 41 of 551 removable words on one run and 105 on a fresh repeat.
The repeat left a thematic bridge in the sponsor read, while the product pitch
itself was selected. Total classifier cost was $0.01638 and $0.00940 respectively.
That variability prevents recommending a production switch.

A fresh Journal episode, “My Monday Morning: Lee Pace,” was transcribed and fully
reviewed before running the frozen v2 prompt. There are 2,305 preserved words,
711 removable words, and 32 unscored guest-thanks words. The candidate selected
zero preserved words on both runs, missing 8 and 18 removable words respectively,
all break handoffs. Costs were $0.00693 and $0.00415. The existing prompt also
selected zero preserved words and removed every labeled commercial; its 117
missed words were credits and a break handoff. This is better boilerplate removal,
not evidence that the candidate catches more audible ads on this episode.

Model-only baseline scores omit production deterministic sponsor guardrails.
Recorded live costs exclude local transcription, audio processing and hosting;
provider prompt-token caching can explain cost variation even with response
caching disabled. All new inference in this directory used dsf without reasoning.

The new Daily recording, “The Fight for the West Bank,” is saved and labeled but
has not been run through these classifiers. Its captured audio has no inserted
commercials. It remains an unused holdout for preservation and boilerplate tests.
Recordings prioritize the user's Daily, Journal and Ologies subscriptions. Other
shows downloaded during exploration were set aside, not added to this evaluation.

## Approach and failed alternatives

First split the transcript at sentence punctuation (with a 65-word length cap).
Classify 35-sentence cores with five sentences of context on either side; retain
each core's decisions to avoid counting overlap twice. Then inspect only boundary
sentences containing coordinated clauses. Clauses split at conjunctions with a
new explicit subject; no sponsor words or reference labels influence splitting.
Local clause decisions can trim a mixed sentence edge but cannot reclassify the
interior of an already-selected block. Whole-boundary disagreement is recorded
and retains the original sentence selection.

Word-ID boundary review and exact-quote boundary review did not repair the mixed
opening sentence reliably. Classifying clauses throughout the entire episode
preserved content but missed more removable material. Their failures are saved,
not discarded in favor of the best run.

Two initial runs stopped because the model omitted required sentence decisions.
The first stopped response was not persisted, so its output/cost cannot be
reconstructed. Later raw responses and failure logs are in records/. The runner
now records missing IDs explicitly and preserves those units, rather than
silently treating an omission as a successful classification. Completed sentence
runs used for the comparisons above have their per-window omissions recorded in
detail.json. All local-boundary response failures likewise retain original edges.

## Reproduction

Run from the repository root in a Python environment containing requests. The
worker extra already supplies it transitively. Each command below makes cheap
API calls to the existing local proxy and refuses an existing output directory.

```sh
uv run --extra worker python experiments/classification/sentences/run.py \
  --case journal_lee_pace \
  --prompt experiments/classification/sentences/prompt-v2.txt \
  --output data/classification-eval/sentences/new-journal

uv run --extra worker python experiments/classification/sentences/local_edges.py \
  --case journal_lee_pace \
  --input data/classification-eval/sentences/new-journal \
  --output data/classification-eval/sentences/new-journal-edges

python3 experiments/classification/evaluate.py score \
  --results experiments/classification/runs/sentence-experiments.json
```

Use --clauses with run.py to reproduce whole-episode clause classification.
refine.py and quote_refine.py reproduce the unsuccessful alternative edge passes.
records/ preserves window decisions, units, response IDs, token usage, costs and
prompt hashes. Aggregate predictions are in ../runs/sentence-experiments.json.
holdout-freeze.json records prompt hashes before the first Journal prediction.
References were authored from transcript review, not listener-adjudicated truth.

## Important boundary finding

A zero transcript-word-loss score missed a real audio problem. The base Journal
transcript omitted conversation between 150.72 and 168.70 seconds. The editor's
assumed-silence extension could delete it even with correct ad selection. The
served production recording also lacks the corresponding exchange. See the
separate ../../slicing/real/journal-gap/ report for the controlled reproduction,
local VAD safeguard and its limitations. That preservation fix is independent of
these classifier experiments and is the practical outcome of this investigation.
