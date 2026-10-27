#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/../.." && pwd)
cd "$root"
export CARGO_BUILD_JOBS=16
out=$(systems/egglog/build c1e12aef4907 systems/egglog/patches/no-dynamic-order.patch)
(cd "$out/src" && cargo test --release -p egglog-core-relations --lib -- --test-threads=1) > logs/paper/core-tests-final.log 2>&1
systems/egglog-storel/build c1e12aef4907 systems/egglog/patches/no-dynamic-order.patch
"$out/egglog" --help | rg 'no-decomp|no-dynamic-order|static-order-refinement'
