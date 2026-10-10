#!/usr/bin/env bash
# Builds every port /bench/ports/<suite>/<name>.ml into the binary /bench/ports/<suite>/<name>.
# A port is a single OCaml file. dune needs module names without dashes, so each file is
# copied under a valid module name into a scratch dune project with a generated stanza.
set -euo pipefail
for suite in /bench/ports/*/; do
  suite=${suite%/}
  scratch=$suite/_dune
  mkdir -p "$scratch"
  printf '(lang dune 3.0)\n' > "$scratch/dune-project"
  : > "$scratch/dune"
  names=()
  for src in "$suite"/*.ml; do
    [ -e "$src" ] || continue
    name=$(basename "$src" .ml); mod=${name//-/_}
    names+=("$name")
    cp "$src" "$scratch/$mod.ml"
    cat >> "$scratch/dune" <<STANZA
(executable (name $mod) (modules $mod)
 (libraries ego containers iter sexplib0 unix)
 (preprocess (pps ppx_deriving.std)))
STANZA
  done
  [ ${#names[@]} -gt 0 ] || { rm -rf "$scratch"; continue; }
  (cd "$scratch" && opam exec -- dune build --profile release)
  for name in "${names[@]}"; do
    cp "$scratch/_build/default/${name//-/_}.exe" "$suite/$name"
  done
  rm -rf "$scratch"
done
