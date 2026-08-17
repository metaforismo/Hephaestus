#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 2 ]]; then
  echo "usage: $0 <rtl-file> <top-module>" >&2
  exit 2
fi

rtl=$1
top=$2
workdir=$(mktemp -d "${TMPDIR:-/tmp}/hephaestus-rtl.XXXXXX")
trap 'rm -rf "$workdir"' EXIT

if command -v iverilog >/dev/null 2>&1; then
  iverilog -g2012 -s "$top" -o "$workdir/parser-check" "$rtl"
else
  echo "warning: iverilog not installed; skipping parser check" >&2
fi

if command -v yosys >/dev/null 2>&1; then
  yosys -q -p "read_verilog -sv $rtl; hierarchy -check -top $top; proc; opt; check; stat"
else
  echo "warning: yosys not installed; skipping synthesis check" >&2
fi
