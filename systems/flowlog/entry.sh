#!/usr/bin/env bash
# Entrypoint of the FlowLog image: run one compiled port and report wall time
# (systems/_container/README.md).
# Usage: entry.sh <suite> <name> [--facts DIR] [--threads N]
# The Containerfile compiled /bench/ports/<suite>/<name>.dl to /bench/bin/<suite>/<name>; this
# runs it with -w N timely workers on the facts in DIR, and translates its stdout
#   <elapsed>:   Dataflow assembled / Dataflow executed   (timer from before the workers start)
#   [size][Rel]  t=()  size=N                               (one per .printsize relation)
# into the port protocol: run_s and ((Rel N) ...). The raw output goes to stderr.
set -euo pipefail
suite=$1; name=$2; shift 2
facts=/facts; threads=1
while [ $# -gt 0 ]; do
  case $1 in
    --facts) facts=$2; shift 2 ;;
    --threads) threads=$2; shift 2 ;;
    *) echo "entry.sh: unsupported flag $1" >&2; exit 3 ;;
  esac
done
bin=/bench/bin/$suite/$name
[ -x "$bin" ] || { echo "entry.sh: no compiled port $bin" >&2; exit 3; }
out=$(mktemp)
# wall_ns = the whole executable: process start, loading the facts, evaluation to fixpoint,
# counting the .printsize relations (the .dl -> Rust -> binary compilation was done at image build).
start=$(date +%s%N)
"$bin" -F "$facts" -D - -w "$threads" > "$out"
end=$(date +%s%N)
cat "$out" >&2
# "<Duration>:\tDataflow executed" (Rust Duration Debug format: 1.5s, 12.3ms, 45µs, 7ns).
awk -F'\t' '$2 == "Dataflow executed" {
  d = $1; sub(/:$/, "", d)
  if (d ~ /ms$/) { sub(/ms$/, "", d); d /= 1000 }
  else if (d ~ /µs$/) { sub(/µs$/, "", d); d /= 1000000 }
  else if (d ~ /ns$/) { sub(/ns$/, "", d); d /= 1000000000 }
  else { sub(/s$/, "", d) }
  printf "run_s %.6f\n", d
}' "$out"
echo "("
sed -nE 's/^\[size\]\[([^]]+)\][[:space:]]+t=\(\)[[:space:]]+size=(-?[0-9]+)$/ (\1 \2)/p' "$out"
echo ")"
rm -f "$out"
echo "wall_ns $((end - start))"
