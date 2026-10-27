#!/usr/bin/env bash
# systems/ggs/entry.sh: run one port inside the image and report wall time.
# Usage: entry.sh <suite> <name> [flags...]
# The port is a standalone SBCL executable /bench/bin/<suite>/<name> built at image build
# time from /bench/ports/<suite>/<name>.lisp (ggs/eqsat preloaded, port precompiled), so the
# wall time below is SBCL core startup plus the port, not Quicklisp/ASDF loading.
set -euo pipefail
suite=$1; name=$2; shift 2
cmd=("/bench/bin/$suite/$name" "$@")
start=$(date +%s%N)
"${cmd[@]}"
end=$(date +%s%N)
echo "wall_ns $((end - start))"
