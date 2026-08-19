#!/usr/bin/env python3
"""Remove declaration-only signedness that OpenSTA cannot parse.

The transform is deliberately narrow: every ``signed`` token in the input must
occur in a single-line Verilog declaration beginning with input, output, inout,
wire, or reg. Any other use fails closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

SIGNED_TOKEN = re.compile(r"\bsigned\b")
SIGNED_DECLARATION = re.compile(
    r"(?P<prefix>[ \t]*(?:input|output|inout|wire|reg)[ \t]+)"
    r"signed(?P<suffix>[ \t]+.*;[ \t]*)"
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sanitize_bytes(raw: bytes) -> tuple[bytes, list[dict[str, Any]]]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("netlist is not valid UTF-8") from exc

    lines = text.splitlines(keepends=True)
    output: list[str] = []
    changes: list[dict[str, Any]] = []

    for number, line in enumerate(lines, start=1):
        body = line.rstrip("\r\n")
        newline = line[len(body) :]
        signed_tokens = SIGNED_TOKEN.findall(body)
        if not signed_tokens:
            output.append(line)
            continue
        if len(signed_tokens) != 1:
            raise ValueError(
                f"line {number} contains {len(signed_tokens)} signed tokens"
            )
        match = SIGNED_DECLARATION.fullmatch(body)
        if match is None:
            raise ValueError(
                f"line {number} uses signed outside a supported declaration: {body!r}"
            )
        sanitized_body = match.group("prefix") + match.group("suffix")
        if SIGNED_TOKEN.search(sanitized_body):
            raise AssertionError("signed token survived a declaration rewrite")
        output.append(sanitized_body + newline)
        changes.append(
            {
                "line": number,
                "before": body,
                "after": sanitized_body,
                "removed_token": "signed",
            }
        )

    if not changes:
        raise ValueError("netlist contains no declaration-only signed tokens")

    sanitized = "".join(output).encode("utf-8")
    if SIGNED_TOKEN.search(sanitized.decode("utf-8")):
        raise AssertionError("sanitized netlist still contains a signed token")
    if len(lines) != len(sanitized.decode("utf-8").splitlines(keepends=True)):
        raise AssertionError("sanitizer changed the line count")
    expected_delta = 6 * len(changes)
    if len(raw) - len(sanitized) != expected_delta:
        raise AssertionError(
            "sanitizer changed bytes beyond the exact signed tokens: "
            f"delta={len(raw) - len(sanitized)} expected={expected_delta}"
        )
    return sanitized, changes


def atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def sanitize_file(netlist: Path, manifest_path: Path) -> dict[str, Any]:
    if not netlist.is_file():
        raise ValueError(f"netlist does not exist: {netlist}")
    if netlist.is_symlink():
        raise ValueError(f"netlist must not be a symlink: {netlist}")

    original = netlist.read_bytes()
    sanitized, changes = sanitize_bytes(original)
    original_copy = netlist.with_suffix(netlist.suffix + ".pre-opensta-compat")
    if original_copy.exists():
        raise ValueError(f"refusing to overwrite existing source copy: {original_copy}")
    atomic_write(original_copy, original)
    atomic_write(netlist, sanitized)

    manifest: dict[str, Any] = {
        "schema": "hephaestus.opensta-verilog-compat.v1",
        "netlist": netlist.name,
        "original_copy": original_copy.name,
        "original": {
            "size_bytes": len(original),
            "sha256": sha256_bytes(original),
        },
        "sanitized": {
            "size_bytes": len(sanitized),
            "sha256": sha256_bytes(sanitized),
        },
        "substitution_count": len(changes),
        "changes": changes,
        "invariants": {
            "only_declaration_signed_tokens_removed": True,
            "line_count_preserved": True,
            "removed_bytes": len(original) - len(sanitized),
            "expected_removed_bytes": 6 * len(changes),
        },
        "claim_boundary": {
            "textual_compatibility_transform_verified": True,
            "functional_equivalence_verified_by_this_transform": False,
            "post_physical_equivalence_verified": False,
        },
    }
    atomic_write(
        manifest_path,
        (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )
    return manifest


def self_test() -> None:
    sample = (
        b"module m(clk, x, y);\n"
        b"  input clk;\n"
        b"  input signed [7:0] x;\n"
        b"  wire signed [7:0] x;\n"
        b"  output signed [7:0] y;\n"
        b"  wire signed [7:0] y;\n"
        b"  assign y = x;\n"
        b"endmodule\n"
    )
    sanitized, changes = sanitize_bytes(sample)
    assert len(changes) == 4
    assert b"signed" not in sanitized
    assert sanitized.count(b"\n") == sample.count(b"\n")
    assert len(sample) - len(sanitized) == 24

    for invalid in (
        b"module m; wire x; assign x = $signed(1'b0); endmodule\n",
        b"module m; wire x; endmodule\n",
        b"module m; input signed signed x; endmodule\n",
    ):
        try:
            sanitize_bytes(invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid sample was accepted: {invalid!r}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("netlist", nargs="?", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--self-test", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.self_test:
        self_test()
        print("OpenSTA compatibility sanitizer self-test passed.")
        return 0
    if args.netlist is None or args.manifest is None:
        raise SystemExit("netlist and --manifest are required")
    manifest = sanitize_file(args.netlist, args.manifest)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
