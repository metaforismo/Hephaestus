#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "usage: $0 OUTPUT_DIR" >&2
  exit 2
fi

root="$(mkdir -p "$1" && cd "$1" && pwd)"
opensta_commit="2b751f0e8196b05ef4ed8246b7e27c63c967ec6d"
cudd_url="https://raw.githubusercontent.com/davidkebo/cudd/main/cudd_versions/cudd-3.0.0.tar.gz"
cudd_sha256="b8e966b4562c96a03e7fbea239729587d7b395d53cadcc39a7203b49cf7eeb69"
cudd_bytes="1175302"

curl --fail --location --retry 3 --retry-all-errors \
  "$cudd_url" --output "$root/cudd-3.0.0.tar.gz"
printf '%s  %s\n' "$cudd_sha256" "$root/cudd-3.0.0.tar.gz" \
  | sha256sum --check --strict
test "$(wc -c < "$root/cudd-3.0.0.tar.gz")" = "$cudd_bytes"
tar -xzf "$root/cudd-3.0.0.tar.gz" -C "$root"
(
  cd "$root/cudd-3.0.0"
  ./configure > "$root/cudd-configure.stdout.txt" 2> "$root/cudd-configure.stderr.txt"
  make -j2 > "$root/cudd-make.stdout.txt" 2> "$root/cudd-make.stderr.txt"
)

source="$root/OpenSTA"
mkdir -p "$source"
git -C "$source" init
git -C "$source" remote add origin https://github.com/parallaxsw/OpenSTA.git
git -C "$source" fetch --depth=1 origin "$opensta_commit"
git -C "$source" checkout --detach FETCH_HEAD
test "$(git -C "$source" rev-parse HEAD)" = "$opensta_commit"

test -f /usr/include/FlexLexer.h
sha256sum /usr/include/FlexLexer.h > "$root/flex-header.sha256.txt"
cmake \
  -S "$source" \
  -B "$source/build" \
  -DCMAKE_BUILD_TYPE=Release \
  -DCUDD_DIR="$root/cudd-3.0.0" \
  -DFLEX_INCLUDE_DIR=/usr/include \
  > "$root/opensta-cmake.stdout.txt" \
  2> "$root/opensta-cmake.stderr.txt"
cmake --build "$source/build" --parallel 2 \
  > "$root/opensta-build.stdout.txt" \
  2> "$root/opensta-build.stderr.txt"
cp "$source/build/sta" "$root/opensta.bin"
chmod +x "$root/opensta.bin"

cat > "$root/smoke.tcl" <<'TCL'
puts "HEPHAESTUS_OPENSTA_SMOKE_OK"
exit 0
TCL
"$root/opensta.bin" "$root/smoke.tcl" \
  > "$root/smoke.stdout.txt" \
  2> "$root/smoke.stderr.txt"
grep -Fx 'HEPHAESTUS_OPENSTA_SMOKE_OK' "$root/smoke.stdout.txt"
ldd "$root/opensta.bin" | sort > "$root/opensta.ldd.txt"
dpkg-query -W -f='${Package}\t${Version}\n' \
  cmake gcc g++ tcl-dev swig bison flex libfl-dev libeigen3-dev \
  libfmt-dev yosys yosys-abc iverilog \
  | sort > "$root/packages.tsv"
yosys -V > "$root/yosys.version.txt"

OPENSTA_ROOT="$root" OPENSTA_COMMIT="$opensta_commit" \
CUDD_URL="$cudd_url" CUDD_SHA256="$cudd_sha256" CUDD_BYTES="$cudd_bytes" \
python - <<'PY'
import hashlib
import json
import os
from pathlib import Path

root = Path(os.environ["OPENSTA_ROOT"])
binary = root / "opensta.bin"
lines = (root / "smoke.stdout.txt").read_text(encoding="utf-8").splitlines()
banner = next((line for line in lines if line.startswith("OpenSTA ")), lines[0])
metadata = {
    "schema": "hephaestus.opensta-tool.v1",
    "repository": "parallaxsw/OpenSTA",
    "commit": os.environ["OPENSTA_COMMIT"],
    "banner": banner,
    "binary": binary.name,
    "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    "binary_reproducibility_verified": False,
    "cudd": {
        "url": os.environ["CUDD_URL"],
        "sha256": os.environ["CUDD_SHA256"],
        "bytes": int(os.environ["CUDD_BYTES"]),
    },
    "flex_header_sha256": (root / "flex-header.sha256.txt").read_text(
        encoding="utf-8"
    ).split()[0],
    "packages_sha256": hashlib.sha256((root / "packages.tsv").read_bytes()).hexdigest(),
    "dynamic_libraries_sha256": hashlib.sha256(
        (root / "opensta.ldd.txt").read_bytes()
    ).hexdigest(),
}
(root / "tool.json").write_text(
    json.dumps(metadata, indent=2, sort_keys=True) + "\n",
    encoding="utf-8",
)
print(json.dumps(metadata, indent=2, sort_keys=True))
PY
