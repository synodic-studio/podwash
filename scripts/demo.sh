#!/usr/bin/env bash
# podwash live demo — three beats, each ending on a real artifact or number.
#
#   1. IT SHIPS          Fetch the live proxy feed off the public instance,
#                        then ffprobe the cleaned MP3 it points at. Ends on
#                        the feed URL, the audio URL, and the measured
#                        runtime the cut actually produced.
#
#   2. TWO BACKENDS      Show the one branch in src/pipeline/classifier.py
#                        that picks between the Anthropic SDK and any
#                        OpenAI-compatible endpoint, then run the real
#                        classify_ads() through the local LiteLLM proxy.
#                        Ends on the segments it found, the elapsed ms from
#                        the pipeline's own ProcessingLog, and the upstream
#                        provider + cost read off the response headers.
#
#   3. THE TOKEN BUDGET  Call the same prompt at two max_tokens values and
#                        print what comes back. Then show the commit and the
#                        tracked comment that record what shrinking the budget
#                        actually did here. Ends on the commit SHA.
#
# Usage:
#   scripts/demo.sh                     interactive, one keypress per beat
#   scripts/demo.sh --auto              start to finish, no interaction
#   scripts/demo.sh --server URL        point beat 1 at another podwash server
#   scripts/demo.sh --proxy URL         point beats 2-3 at another OpenAI-
#                                       compatible endpoint (must end in /v1)
#   scripts/demo.sh --cleanup           delete .demo-out/ (this repo only)
#   scripts/demo.sh -h                  print this header
#
# Settings: scripts/demo.env (gitignored), documented in scripts/demo.env.example.
# Every key has a default, so only the differences need setting.
#
# Offline / hostile network:
#   - Public instance unreachable  -> beat 1 falls back to the local SQLite
#                                     feed rows and says the fetch failed.
#   - LiteLLM proxy down           -> beats 2-3 print the request they would
#                                     have sent and the committed evidence,
#                                     and keep going. Nothing is faked.
#   - Point --proxy at a laptop-local proxy if the venue blocks egress.
#
# First-run checklist, do these BEFORE the live run:
#   - uv sync --extra worker --extra dev   (classify_ads imports `anthropic`)
#   - ffprobe on PATH                      (brew install ffmpeg)
#   - the LiteLLM proxy answering on localhost:4000, `small` alias routable
#   - one --auto pass, so first-run model/tool downloads are already paid for
#
# One rehearsal artifact worth knowing about: the LiteLLM proxy caches
# responses, and beat 2 calls production code that cannot opt out, so after the
# first run its ProcessingLog drops to tens of ms. The script detects that and
# prints the run's real upstream latency next to it — read that line out rather
# than letting a 30ms round trip pass as a fast model. Beat 2's routing probe
# and both of beat 3's calls send no-cache, so those are always live.
#
# Run it once with --auto, then --cleanup, so the first live run is not the
# first run.

# Deliberately no `set -e`. One failed beat should print its own failure and
# let the rest of the deck run; aborting mid-demo is worse than a red line.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

# ---------------------------------------------------------------- appearance
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  B=$'\033[1m'; D=$'\033[2m'; R=$'\033[0m'
  G=$'\033[32m'; Y=$'\033[33m'; C=$'\033[36m'; M=$'\033[31m'
else
  B=""; D=""; R=""; G=""; Y=""; C=""; M=""
fi

beat()  { printf '\n%s%s── %s %s%s\n\n' "$C" "$B" "$1" "$(printf '%.0s─' $(seq 1 $((60 - ${#1}))))" "$R"; }
cmd()   { printf '%s%s$ %s%s\n' "$B" "$D" "$1" "$R"; }
warn()  { printf '%s%s!! %s%s\n' "$Y" "$B" "$1" "$R" >&2; }
bad()   { printf '%sxx %s%s\n' "$M" "$1" "$R" >&2; }
ok()    { printf '%s%s%s\n' "$G" "$1" "$R"; }
note()  { printf '%s%s%s\n' "$D" "$1" "$R"; }

# Anything the operator has to do or say gets its own voice, so it cannot be
# mistaken for output.
cue() {
  printf '\n%s%s   %s%s\n' "$Y" "$B" "$1" "$R"
  printf '%s%s   %s%s\n' "$Y" "$D" "$2" "$R"
}

advance() {
  [ -n "${AUTO:-}" ] && return 0
  printf '\n%s   [ %s ]%s' "$D" "$1" "$R"
  read -n 1 -s -r _ <&3
  printf '\r%*s\r' $((${#1} + 12)) ""
}

# ------------------------------------------------------------------- options
AUTO=""; CLEANUP=""
SERVER_ARG=""; PROXY_ARG=""

while [ $# -gt 0 ]; do
  case "$1" in
    --auto)    AUTO=1 ;;
    --cleanup) CLEANUP=1 ;;
    --server)  SERVER_ARG="${2:-}"; shift ;;
    --proxy)   PROXY_ARG="${2:-}"; shift ;;
    -h|--help)
      sed -n '2,/^$/p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
      exit 0 ;;
    *) bad "unknown option: $1 (try -h)"; exit 2 ;;
  esac
  shift
done

# ------------------------------------------------------------------ settings
# shellcheck disable=SC1091
[ -f scripts/demo.env ] && . scripts/demo.env

SERVER="${SERVER_ARG:-${PODWASH_DEMO_SERVER:-https://podcast.kj6.dev}}"
FEED="${PODWASH_DEMO_FEED:-the-daily}"
PROXY="${PROXY_ARG:-${PODWASH_DEMO_PROXY:-http://localhost:4000/v1}}"
# The cheap tier, always. A demo has no business spending on a big model, and
# on a 429 the right move is to retry this same alias, not to escalate.
MODEL="${PODWASH_DEMO_MODEL:-small}"
# Bearer token for a proxy that requires one. Read, never printed.
DEMO_KEY="${PODWASH_DEMO_KEY:-}"
SERVER="${SERVER%/}"; PROXY="${PROXY%/}"

OUT_ROOT="$REPO/.demo-out"

# ------------------------------------------------------------------- cleanup
if [ -n "$CLEANUP" ]; then
  # Refuse to delete anything that is not literally this repo's own output dir.
  case "$OUT_ROOT" in
    "$REPO/.demo-out") ;;
    *) bad "refusing to remove '$OUT_ROOT'"; exit 2 ;;
  esac
  if [ -d "$OUT_ROOT" ]; then
    find "$OUT_ROOT" -mindepth 1 -maxdepth 1 -type d | sed "s|^|removed |"
    rm -rf "$OUT_ROOT" && ok "cleaned $OUT_ROOT"
  else
    note "nothing to clean: $OUT_ROOT does not exist"
  fi
  exit 0
fi

# --------------------------------------------------------------- tty guard
# Without a tty every read returns instantly and all three beats scroll past in
# one second, which looks exactly like the demo crashing.
if [ -z "$AUTO" ]; then
  if (exec 3</dev/tty) 2>/dev/null; then
    exec 3</dev/tty
  else
    warn "No terminal to read keypresses from, so every beat would run at once."
    warn "Run it from a terminal, or use --auto to run the whole thing."
    exit 1
  fi
fi

# ------------------------------------------------------------------ preflight
beat "PREFLIGHT"

HAVE_CURL=""; HAVE_PY=""; HAVE_FFPROBE=""; HAVE_UV=""
HAVE_CLASSIFIER=""; HAVE_SERVER=""; HAVE_PROXY=""; HAVE_GIT=""

if [ ! -f prompts/ad_detection.txt ] || [ ! -f src/pipeline/classifier.py ]; then
  bad "not a podwash checkout: $REPO"
  exit 2
fi
ok "repo            $REPO"

command -v curl    >/dev/null 2>&1 && HAVE_CURL=1
command -v python3 >/dev/null 2>&1 && HAVE_PY=1
command -v ffprobe >/dev/null 2>&1 && HAVE_FFPROBE=1
command -v uv      >/dev/null 2>&1 && HAVE_UV=1
git rev-parse --git-dir >/dev/null 2>&1 && HAVE_GIT=1

[ -n "$HAVE_CURL" ]    && ok "curl            $(command -v curl)"        || warn "curl missing: beat 1 cannot fetch the feed"
[ -n "$HAVE_PY" ]      && ok "python3         $(command -v python3)"     || warn "python3 missing: beat 1 cannot parse the feed"
[ -n "$HAVE_FFPROBE" ] && ok "ffprobe         $(command -v ffprobe)"     || warn "ffprobe missing: beat 1 cannot measure the cleaned audio"
[ -n "$HAVE_UV" ]      && ok "uv              $(command -v uv)"          || warn "uv missing: beats 2-3 cannot run the pipeline code"
[ -n "$HAVE_GIT" ]     && ok "git             $(git rev-parse --short HEAD 2>/dev/null)" || warn "not a git checkout: beat 3 falls back to the source comment"

if [ -n "$HAVE_UV" ]; then
  if uv run --quiet python -c 'import src.pipeline.classifier' >/dev/null 2>&1; then
    HAVE_CLASSIFIER=1
    ok "classify_ads    importable"
  else
    warn "src.pipeline.classifier will not import — run: uv sync --extra worker --extra dev"
  fi
fi

if [ -n "$HAVE_CURL" ]; then
  HEALTH="$(curl -fsS -m 10 "$SERVER/health" 2>&1)"
  if [ $? -eq 0 ]; then
    HAVE_SERVER=1
    ok "podwash server  $SERVER  $HEALTH"
  else
    warn "podwash server unreachable at $SERVER"
    warn "  $HEALTH"
    warn "  beat 1 will fall back to the local database."
  fi

  # Two spellings rather than an array: bash 3.2 ships on macOS, and an empty
  # array expanded under `set -u` aborts the script.
  if [ -n "$DEMO_KEY" ]; then
    MODELS="$(curl -fsS -m 10 -H "Authorization: Bearer $DEMO_KEY" "$PROXY/models" 2>&1)"
  else
    MODELS="$(curl -fsS -m 10 "$PROXY/models" 2>&1)"
  fi
  if [ $? -eq 0 ]; then
    HAVE_PROXY=1
    N_MODELS="$(printf '%s' "$MODELS" | tr ',' '\n' | grep -c '"id"')"
    ok "litellm proxy   $PROXY  ($N_MODELS aliases)"
    if printf '%s' "$MODELS" | grep -q "\"id\":\"$MODEL\""; then
      ok "model alias     $MODEL  (routable)"
    else
      warn "alias '$MODEL' is not in this proxy's model list; beats 2-3 will fail loudly"
    fi
  else
    warn "LiteLLM proxy unreachable at $PROXY"
    warn "  $MODELS"
    warn "  beats 2-3 will show the request and the committed evidence, but no live call."
  fi
fi

RUN="$OUT_ROOT/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RUN"
note "artifacts       $RUN"

cue "You are the operator. Every number after this came off the wire just now." \
    "If a beat prints nothing, the thing behind it is genuinely down — say so."
advance "press to start beat 1"

# =============================================================== BEAT 1 ======
beat "1 / IT SHIPS — a live feed and the audio it points at"

FEED_XML="$RUN/feed.xml"

if [ -n "$HAVE_SERVER" ] && [ -n "$HAVE_PY" ]; then
  cmd "curl -sS $SERVER/feeds/$FEED.xml"
  CODE="$(curl -sS -m 30 -o "$FEED_XML" -w '%{http_code} %{size_download} %{time_total}' "$SERVER/feeds/$FEED.xml")"
  set -- $CODE
  printf 'HTTP %s   %s bytes   %ss\n\n' "$1" "$2" "$3"

  if [ "$1" = "200" ]; then
    FEED_XML="$FEED_XML" HAVE_FFPROBE="$HAVE_FFPROBE" python3 scripts/demo_feed.py
  else
    bad "server answered $1; nothing to measure"
  fi
else
  warn "no live feed to fetch — falling back to what this checkout knows"
  if command -v sqlite3 >/dev/null 2>&1 && [ -f data/podwash.db ]; then
    Q="select slug, name, case enabled when 1 then 'enabled' else 'off' end as state from feeds"
    cmd "sqlite3 -column -header data/podwash.db \"$Q\""
    sqlite3 -column -header data/podwash.db "$Q"
  else
    note "no local data/podwash.db either; beat 1 has nothing to show."
  fi
fi

advance "press when they have had a look at the URLs"

# =============================================================== BEAT 2 ======
beat "2 / TWO BACKENDS — one branch, two vendors"

cmd "grep -n 'backend ==' -A 12 src/pipeline/classifier.py"
grep -n 'if backend == "claude"' -A 12 src/pipeline/classifier.py

printf '\n'
note "scripts/demo-transcript.json — a sample in the shape Whisper emits:"
python3 - <<'PY' 2>/dev/null || head -c 400 scripts/demo-transcript.json
import json
segs = json.load(open("scripts/demo-transcript.json"))
for s in segs[:5]:
    print(f'[{s["start"]:>6.1f}s - {s["end"]:>6.1f}s]{s["text"]}')
print(f'... {len(segs)} segments, {segs[-1]["end"]:.0f}s of transcript')
PY

printf '\n'
if [ -n "$HAVE_CLASSIFIER" ]; then
  cmd "uv run python scripts/demo_classify.py"
  RUN="$RUN" PROXY="$PROXY" MODEL="$MODEL" DEMO_KEY="$DEMO_KEY" HAVE_PROXY="$HAVE_PROXY" \
    uv run --quiet python scripts/demo_classify.py
else
  warn "cannot run the pipeline code here; this is the request it would send:"
  note "  POST $PROXY/chat/completions"
  note "  model=$MODEL  max_tokens=<litellm.max_tokens>  thinking=disabled"
  note "  prompt = prompts/ad_detection.txt with the transcript interpolated ($(wc -c < prompts/ad_detection.txt | tr -d ' ') bytes of instructions)"
  printf '\n'
  cmd "grep -n 'returned empty content' -B 4 -A 4 src/pipeline/classifier.py"
  grep -n 'returned empty content' -B 4 -A 4 src/pipeline/classifier.py
fi

advance "press when done on the routing header"

# =============================================================== BEAT 3 ======
beat "3 / THE TOKEN BUDGET — a failure with no signal"

if [ -n "$HAVE_CLASSIFIER" ] && [ -n "$HAVE_PROXY" ]; then
  cmd "uv run python scripts/demo_budget.py"
  RUN="$RUN" PROXY="$PROXY" MODEL="$MODEL" DEMO_KEY="$DEMO_KEY" \
    uv run --quiet python scripts/demo_budget.py
else
  warn "no live proxy — skipping the two live calls, showing the record instead"
fi

printf '\n'
if [ -n "$HAVE_GIT" ]; then
  SHA="$(git log -1 --format=%h --grep='classifier token budget' 2>/dev/null)"
  if [ -n "$SHA" ]; then
    cmd "git show -s --format='%h %s%n%n%b' $SHA"
    git show -s --format='%h %s%n%n%b' "$SHA" | grep -v '^Co-Authored-By:\|^Claude-Session:'
  else
    warn "no commit matching 'classifier token budget' in this checkout"
  fi
fi

cmd "grep -n 'sizes its answer' -B 6 -A 4 src/config.py"
grep -n 'sizes its answer' -B 6 -A 4 src/config.py

printf '\n'
if [ -n "$HAVE_GIT" ] && [ -n "${SHA:-}" ]; then
  ok "that is the whole fix: $SHA — one default, $(git show --format='' --shortstat "$SHA" | tr -d '\n' | sed 's/^ *//')"
fi

cue "The commit's measurements: 4096 -> 2 segments, 8192 -> 3, 16000 -> 4." \
    "Valid JSON every time, no truncation signal, ~350 tokens either way — a quality knob wearing a cost knob's name."

advance "press to finish"

# =================================================================== CLOSE ===
beat "ARTIFACTS"
if [ -n "$(find "$RUN" -type f 2>/dev/null)" ]; then
  find "$RUN" -type f | sort | sed 's|^|  |'
  printf '\n'
  note "clear them with: scripts/demo.sh --cleanup"
else
  warn "nothing was produced: every backend this demo talks to was unreachable."
fi
