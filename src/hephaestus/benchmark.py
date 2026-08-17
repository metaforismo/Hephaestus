"""Reproducible evidence runner for compiled constant-matrix experiments.

The runner deliberately reports structural compiler evidence separately from physical
implementation evidence. It never upgrades an RTL result into a PPA, PEX, or silicon claim.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from dataclasses import asdict, dataclass
from importlib import metadata
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = 1


class BenchmarkError(RuntimeError):
    """Raised when a benchmark case cannot produce trustworthy evidence."""


@dataclass(frozen=True)
class StructuralMetrics:
    rows: int
    columns: int
    coefficients: int
    nonzero_terms: int
    coefficient_density: float
    naive_adders: int
    compiled_adders: int | None
    adders_saved: int | None
    adder_reduction_fraction: float | None
    max_depth: int | None
    max_fanout: int | None
    runtime_weight_reads_per_matvec: int | None
    weighted_quantization_mse: float | None


@dataclass(frozen=True)
class CaseEvidence:
    name: str
    source: str
    module: str
    status: str
    compiler_version: str
    metrics: StructuralMetrics
    claims: dict[str, bool]
    artifacts: dict[str, str]
    source_sha256: str
    artifact_sha256: dict[str, str]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_key(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _deep_values(document: Any, candidate_keys: set[str]) -> list[Any]:
    normalized = {_normalized_key(key) for key in candidate_keys}
    values: list[Any] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if _normalized_key(str(key)) in normalized:
                    values.append(child)
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return values


def _first_number(document: Any, *keys: str) -> int | float | None:
    for value in _deep_values(document, set(keys)):
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float)):
            return value
    return None


def _first_bool(document: Any, *keys: str) -> bool | None:
    for value in _deep_values(document, set(keys)):
        if isinstance(value, bool):
            return value
    return None


def _count_adder_nodes(plan: Any) -> int | None:
    """Count additions while tolerating small IR schema revisions."""

    if not isinstance(plan, dict):
        return None

    for key in ("nodes", "addition_nodes", "adder_nodes"):
        nodes = plan.get(key)
        if not isinstance(nodes, list):
            continue

        explicit = 0
        structurally_binary = 0
        for node in nodes:
            if not isinstance(node, dict):
                continue
            operation = str(node.get("op", node.get("operation", ""))).lower()
            if operation in {"add", "addition", "+", "sub", "subtract", "-"}:
                explicit += 1
            if any(
                left in node and right in node
                for left, right in (("left", "right"), ("lhs", "rhs"), ("a", "b"))
            ):
                structurally_binary += 1

        if explicit:
            return explicit
        if structurally_binary:
            return structurally_binary
        # The current ZeroFetch plan stores additions only in its nodes collection.
        return len(nodes)

    return None


def _compiler_version() -> str:
    for distribution in ("hephaestus-compiler", "hephaestus"):
        try:
            return metadata.version(distribution)
        except metadata.PackageNotFoundError:
            continue
    return "unknown"


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchmarkError(f"cannot read JSON artifact {path}: {exc}") from exc


def _logical_baseline(codes: np.ndarray) -> tuple[int, int, float]:
    if codes.ndim != 2:
        raise BenchmarkError(f"expected a 2-D code matrix, received shape {codes.shape}")

    nonzero = int(np.count_nonzero(codes))
    per_row = np.count_nonzero(codes, axis=1)
    naive_adders = int(np.maximum(per_row - 1, 0).sum())
    density = float(nonzero / codes.size) if codes.size else 0.0
    return nonzero, naive_adders, density


def _extract_claims(manifest: Any) -> dict[str, bool]:
    bit_exact = _first_bool(
        manifest,
        "bit_exact_integer_core_verified",
        "bit_exact_verified",
        "verification_passed",
    )
    return {
        "bit_exact_integer_core_verified": bool(bit_exact),
        "post_synthesis_ppa_measured": bool(_first_bool(manifest, "post_synthesis_ppa_measured")),
        "post_layout_pex_verified": bool(_first_bool(manifest, "post_layout_pex_verified")),
        "silicon_verified": bool(_first_bool(manifest, "silicon_verified")),
    }


def _case_metrics(codes: np.ndarray, plan: Any, manifest: Any) -> StructuralMetrics:
    rows, columns = (int(value) for value in codes.shape)
    nonzero, naive_adders, density = _logical_baseline(codes)

    compiled_value = _first_number(
        manifest,
        "compiled_adders",
        "adder_nodes",
        "addition_nodes",
        "adders",
    )
    compiled_adders = (
        int(compiled_value) if compiled_value is not None else _count_adder_nodes(plan)
    )

    saved = None if compiled_adders is None else naive_adders - compiled_adders
    reduction = None
    if saved is not None:
        reduction = float(saved / naive_adders) if naive_adders else 0.0

    depth_value = _first_number(manifest, "max_depth", "adder_depth", "depth")
    fanout_value = _first_number(manifest, "max_fanout", "fanout_maximum")
    reads_value = _first_number(
        manifest,
        "runtime_weight_reads_per_matvec",
        "runtime_weight_reads",
        "runtime_coefficient_reads",
    )
    mse_value = _first_number(
        manifest,
        "weighted_quantization_mse",
        "weighted_mse",
        "quantization_mse",
        "mse",
    )

    return StructuralMetrics(
        rows=rows,
        columns=columns,
        coefficients=int(codes.size),
        nonzero_terms=nonzero,
        coefficient_density=density,
        naive_adders=naive_adders,
        compiled_adders=compiled_adders,
        adders_saved=saved,
        adder_reduction_fraction=reduction,
        max_depth=int(depth_value) if depth_value is not None else None,
        max_fanout=int(fanout_value) if fanout_value is not None else None,
        runtime_weight_reads_per_matvec=(int(reads_value) if reads_value is not None else None),
        weighted_quantization_mse=float(mse_value) if mse_value is not None else None,
    )


def _resolve_source(repo_root: Path, suite_path: Path, source: str) -> Path:
    candidate = Path(source)
    if not candidate.is_absolute():
        candidate = repo_root / candidate
    resolved = candidate.resolve()
    if not resolved.is_file():
        raise BenchmarkError(
            f"benchmark source does not exist: {source} (suite: {suite_path.as_posix()})"
        )
    return resolved


def _compile_case(
    *,
    case: dict[str, Any],
    suite_path: Path,
    repo_root: Path,
    output_root: Path,
) -> CaseEvidence:
    name = str(case.get("name", "")).strip()
    source_value = str(case.get("source", "")).strip()
    module = str(case.get("module", f"hephaestus_{name}")).strip()
    if not name or not source_value or not module:
        raise BenchmarkError("each case requires non-empty name, source, and module fields")

    verify_samples = int(case.get("verify_samples", 256))
    if verify_samples <= 0:
        raise BenchmarkError(f"case {name!r} requires verify_samples > 0")

    source = _resolve_source(repo_root, suite_path, source_value)
    case_output = output_root / name
    case_output.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "hephaestus",
        "compile",
        str(source),
        "--out",
        str(case_output),
        "--module",
        module,
        "--verify-samples",
        str(verify_samples),
    ]
    tensor_key = case.get("tensor_key")
    if tensor_key:
        command.extend(["--tensor-key", str(tensor_key)])

    completed = subprocess.run(  # noqa: S603
        command,
        cwd=repo_root,
        check=False,
        text=True,
        capture_output=True,
    )
    (case_output / "compile.stdout.txt").write_text(completed.stdout, encoding="utf-8")
    (case_output / "compile.stderr.txt").write_text(completed.stderr, encoding="utf-8")
    if completed.returncode != 0:
        raise BenchmarkError(
            f"compiler failed for case {name!r} with exit code {completed.returncode}; "
            f"see {case_output / 'compile.stderr.txt'}"
        )

    required = {
        "manifest": case_output / "manifest.json",
        "plan": case_output / "plan.json",
        "codes": case_output / "codes.npy",
        "rtl": case_output / f"{module}.sv",
    }
    missing = [label for label, path in required.items() if not path.is_file()]
    if missing:
        raise BenchmarkError(f"case {name!r} is missing artifacts: {', '.join(missing)}")

    manifest = _load_json(required["manifest"])
    plan = _load_json(required["plan"])
    try:
        codes = np.load(required["codes"], allow_pickle=False)
    except (OSError, ValueError) as exc:
        raise BenchmarkError(f"cannot read quantized codes for {name!r}: {exc}") from exc

    metrics = _case_metrics(codes, plan, manifest)
    claims = _extract_claims(manifest)

    if bool(case.get("require_bit_exact", True)) and not claims["bit_exact_integer_core_verified"]:
        raise BenchmarkError(f"case {name!r} did not prove bit-exact integer-core equivalence")
    if bool(case.get("require_zero_weight_reads", True)):
        reads = metrics.runtime_weight_reads_per_matvec
        if reads is None:
            raise BenchmarkError(f"case {name!r} did not report runtime weight reads")
        if reads != 0:
            raise BenchmarkError(f"case {name!r} reported {reads} runtime weight reads")

    artifacts = {
        label: path.relative_to(output_root).as_posix() for label, path in required.items()
    }
    hashes = {label: _sha256(path) for label, path in required.items()}

    return CaseEvidence(
        name=name,
        source=source.relative_to(repo_root).as_posix(),
        module=module,
        status="verified",
        compiler_version=_compiler_version(),
        metrics=metrics,
        claims=claims,
        artifacts=artifacts,
        source_sha256=_sha256(source),
        artifact_sha256=hashes,
    )


def _markdown_report(cases: list[CaseEvidence]) -> str:
    lines = [
        "# Hephaestus structural evidence",
        "",
        "This report contains compiler and RTL-level evidence only.",
        "It does not claim post-synthesis PPA, post-layout extraction, or measured silicon.",
        "",
        (
            "| Case | Shape | Nonzero | Naive adders | Compiled adders | Saved | "
            "Max depth | Max fanout | Runtime weight reads | Bit-exact |"
        ),
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|:---:|",
    ]
    for case in cases:
        metrics = case.metrics
        values = (
            case.name,
            f"{metrics.rows}×{metrics.columns}",
            str(metrics.nonzero_terms),
            str(metrics.naive_adders),
            "n/a" if metrics.compiled_adders is None else str(metrics.compiled_adders),
            "n/a" if metrics.adders_saved is None else str(metrics.adders_saved),
            "n/a" if metrics.max_depth is None else str(metrics.max_depth),
            "n/a" if metrics.max_fanout is None else str(metrics.max_fanout),
            (
                "n/a"
                if metrics.runtime_weight_reads_per_matvec is None
                else str(metrics.runtime_weight_reads_per_matvec)
            ),
            "yes" if case.claims["bit_exact_integer_core_verified"] else "no",
        )
        lines.append("| " + " | ".join(values) + " |")

    lines.extend(
        [
            "",
            "## Claim boundary",
            "",
            "For every case in this report:",
            "",
            "- `post_synthesis_ppa_measured = false` unless an independent future flow proves it;",
            "- `post_layout_pex_verified = false` unless extracted verification exists;",
            "- `silicon_verified = false` until a fabricated device is measured.",
            "",
        ]
    )
    return "\n".join(lines)


def run_suite(suite: Path, output: Path, repo_root: Path) -> dict[str, Any]:
    suite_path = suite.resolve()
    repo = repo_root.resolve()
    output_root = output.resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    document = _load_json(suite_path)
    if not isinstance(document, dict) or int(document.get("schema_version", 0)) != SCHEMA_VERSION:
        raise BenchmarkError(f"suite must use schema_version {SCHEMA_VERSION}")
    raw_cases = document.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        raise BenchmarkError("suite must contain a non-empty cases list")

    names: set[str] = set()
    evidence: list[CaseEvidence] = []
    for raw_case in raw_cases:
        if not isinstance(raw_case, dict):
            raise BenchmarkError("every suite case must be a JSON object")
        name = str(raw_case.get("name", ""))
        if name in names:
            raise BenchmarkError(f"duplicate benchmark case name: {name!r}")
        names.add(name)
        evidence.append(
            _compile_case(
                case=raw_case,
                suite_path=suite_path,
                repo_root=repo,
                output_root=output_root,
            )
        )

    result = {
        "schema_version": SCHEMA_VERSION,
        "suite": suite_path.relative_to(repo).as_posix(),
        "evidence_level": "algorithmic_and_rtl",
        "cases": [asdict(case) for case in evidence],
        "claims": {
            "post_synthesis_ppa_measured": False,
            "post_layout_pex_verified": False,
            "silicon_verified": False,
        },
    }
    (output_root / "evidence.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (output_root / "SUMMARY.md").write_text(_markdown_report(evidence), encoding="utf-8")
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a deterministic matrix suite and collect structural evidence."
    )
    parser.add_argument("suite", type=Path, help="Path to the benchmark suite JSON")
    parser.add_argument("--out", type=Path, default=Path("build/evidence"))
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    try:
        result = run_suite(arguments.suite, arguments.out, arguments.repo_root)
    except BenchmarkError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    print(
        f"verified {len(result['cases'])} case(s); "
        f"evidence: {(arguments.out / 'evidence.json').resolve()}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
