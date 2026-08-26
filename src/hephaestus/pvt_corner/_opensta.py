"""Pinned OpenSTA script generation, execution, and raw-report replay."""

from __future__ import annotations

import math
import re
import subprocess
from pathlib import Path
from typing import Any

from ._common import PVTCornerError, require_positive_int, sha256_file

_NUMBER = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"
_SLACK_RE = re.compile(
    rf"^\s*({_NUMBER})\s+slack\s+"
    r"\((MET|VIOLATED)\)\s*$",
    re.MULTILINE,
)
_WORST_SLACK_RE = re.compile(
    rf"^\s*worst\s+slack\s+max\s+({_NUMBER})\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_TNS_RE = re.compile(
    rf"^\s*tns(?:\s+max)?\s+({_NUMBER})\s*$",
    re.MULTILINE | re.IGNORECASE,
)
_CLOCK_PERIOD_RE = re.compile(
    r"(?P<prefix>\bcreate_clock\b[^\n]*?\s-period\s+)"
    r"(?P<period>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)"
)
_CORNER_MARKER_RE = re.compile(r"^HEPHAESTUS_PVT_CORNER=(\S+)$", re.MULTILINE)
_DONE_MARKER = "HEPHAESTUS_PVT_DONE=1"
_FATAL_RE = re.compile(r"(?m)^\s*(?:Error:|%Error|FATAL:)")


def tighten_sdc(text: str, period_ns: float) -> str:
    """Replace exactly one create_clock period for the timing negative control."""

    if not math.isfinite(period_ns) or period_ns <= 0:
        raise ValueError("tight clock period must be finite and positive")
    matches = list(_CLOCK_PERIOD_RE.finditer(text))
    if len(matches) != 1:
        raise PVTCornerError(
            "SDC must contain exactly one replaceable create_clock period"
        )
    replacement = rf"\g<prefix>{period_ns:.12g}"
    return _CLOCK_PERIOD_RE.sub(replacement, text, count=1)


def _tcl_path(path: Path, *, context: str) -> str:
    value = path.resolve().as_posix()
    if "{" in value or "}" in value or "\n" in value or "\r" in value:
        raise PVTCornerError(f"{context} path cannot be represented safely in Tcl")
    return "{" + value + "}"


def emit_opensta_script(
    *,
    liberty: Path,
    netlist: Path,
    top_module: str,
    sdc: Path,
    spef: Path,
    corner_label: str,
) -> str:
    """Emit the exact routed OpenSTA analysis script."""

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", top_module):
        raise PVTCornerError("top module is not a safe Verilog identifier")
    if not re.fullmatch(r"[a-z0-9_-]+", corner_label):
        raise PVTCornerError("corner label is unsafe")
    return "\n".join(
        [
            "sta::set_sta_continue_on_error 0",
            f"read_liberty {_tcl_path(liberty, context='Liberty')}",
            f"read_verilog {_tcl_path(netlist, context='netlist')}",
            f"link_design {top_module}",
            f"read_sdc {_tcl_path(sdc, context='SDC')}",
            f"read_spef {_tcl_path(spef, context='SPEF')}",
            "check_setup",
            "set_propagated_clock [all_clocks]",
            f'puts "HEPHAESTUS_PVT_CORNER={corner_label}"',
            "report_checks -path_delay max -group_count 5 -endpoint_count 1",
            "report_worst_slack -max",
            "report_tns",
            f'puts "{_DONE_MARKER}"',
            "exit 0",
            "",
        ]
    )


def _parse_timing_metrics(stdout: str) -> tuple[float, str, float]:
    """Parse the pinned OpenSTA summaries with a fixture-compatible fallback."""

    worst_matches = _WORST_SLACK_RE.findall(stdout)
    if len(worst_matches) > 1:
        raise PVTCornerError("OpenSTA output contains multiple worst-slack summaries")

    check_matches = _SLACK_RE.findall(stdout)
    if worst_matches:
        slack = float(worst_matches[0])
        status = "violated" if slack < 0 else "met"
        if not check_matches:
            raise PVTCornerError("OpenSTA output lacks a parseable setup path")
    else:
        # The fallback preserves the small executable fixture used by the unit
        # suite. Real scripts always emit report_worst_slack above.
        if not check_matches:
            raise PVTCornerError("OpenSTA output lacks a parseable setup slack")
        slack_text, raw_status = check_matches[-1]
        slack = float(slack_text)
        status = raw_status.lower()

    tns_matches = _TNS_RE.findall(stdout)
    if len(tns_matches) != 1:
        raise PVTCornerError(
            "OpenSTA output must contain exactly one total-negative-slack summary"
        )
    tns = float(tns_matches[0])
    if not math.isfinite(slack) or not math.isfinite(tns):
        raise PVTCornerError("OpenSTA returned a non-finite timing metric")
    if (slack < 0 and status != "violated") or (slack >= 0 and status != "met"):
        raise PVTCornerError("OpenSTA slack sign and status disagree")
    if tns > 0:
        raise PVTCornerError("OpenSTA total negative slack cannot be positive")
    if slack >= 0 and not math.isclose(tns, 0.0, rel_tol=0.0, abs_tol=1e-12):
        raise PVTCornerError(
            "OpenSTA reports negative total slack while worst setup slack is met"
        )
    if slack < 0 and tns >= 0:
        raise PVTCornerError(
            "OpenSTA reports no negative total slack for a violated setup path"
        )
    return slack, status, tns


def parse_opensta_output(
    stdout: str,
    stderr: str,
    *,
    expected_label: str,
) -> dict[str, Any]:
    """Parse a complete raw OpenSTA report and reject partial or fatal output."""

    combined = stdout + "\n" + stderr
    if _DONE_MARKER not in stdout:
        raise PVTCornerError("OpenSTA output lacks the completion marker")
    labels = _CORNER_MARKER_RE.findall(stdout)
    if labels != [expected_label]:
        raise PVTCornerError(
            f"OpenSTA corner marker differs: expected {expected_label!r}, got {labels}"
        )
    fatal = _FATAL_RE.search(combined)
    if fatal is not None:
        line = combined[fatal.start() :].splitlines()[0]
        raise PVTCornerError(f"OpenSTA reported a fatal diagnostic: {line}")
    slack, status, tns = _parse_timing_metrics(stdout)
    return {
        "worst_setup_slack_ns": slack,
        "slack_status": status,
        "total_negative_slack_ns": tns,
    }


def metrics_equal(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if left.get("slack_status") != right.get("slack_status"):
        return False
    for name in ("worst_setup_slack_ns", "total_negative_slack_ns"):
        lhs = left.get(name)
        rhs = right.get(name)
        if type(lhs) not in (int, float) or type(rhs) not in (int, float):
            return False
        if not math.isclose(float(lhs), float(rhs), rel_tol=0.0, abs_tol=1e-9):
            return False
    return True


def run_opensta(
    *,
    executable: Path,
    workdir: Path,
    script: str,
    label: str,
    replay: int,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one isolated OpenSTA analysis and preserve all replay inputs/outputs."""

    require_positive_int(replay, context="OpenSTA replay")
    timeout = require_positive_int(timeout_seconds, context="OpenSTA timeout")
    workdir.mkdir(parents=True, exist_ok=False)
    script_path = workdir / "analysis.tcl"
    stdout_path = workdir / "stdout.txt"
    stderr_path = workdir / "stderr.txt"
    returncode_path = workdir / "returncode.txt"
    script_path.write_text(script, encoding="utf-8")
    try:
        completed = subprocess.run(
            [str(executable.resolve()), str(script_path.resolve())],
            cwd=workdir,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout
        stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr
        stdout_path.write_text(stdout or "", encoding="utf-8")
        stderr_path.write_text(stderr or "", encoding="utf-8")
        returncode_path.write_text("timeout\n", encoding="utf-8")
        raise PVTCornerError(f"OpenSTA replay {replay} timed out") from exc
    stdout_path.write_text(completed.stdout, encoding="utf-8")
    stderr_path.write_text(completed.stderr, encoding="utf-8")
    returncode_path.write_text(f"{completed.returncode}\n", encoding="utf-8")
    if completed.returncode != 0:
        raise PVTCornerError(
            f"OpenSTA replay {replay} failed with return code {completed.returncode}"
        )
    metrics = parse_opensta_output(
        completed.stdout,
        completed.stderr,
        expected_label=label,
    )
    return {
        "replay": replay,
        "returncode": completed.returncode,
        "metrics": metrics,
        "script": script_path.name,
        "script_sha256": sha256_file(script_path),
        "stdout": stdout_path.name,
        "stdout_sha256": sha256_file(stdout_path),
        "stderr": stderr_path.name,
        "stderr_sha256": sha256_file(stderr_path),
        "returncode_file": returncode_path.name,
        "returncode_file_sha256": sha256_file(returncode_path),
    }


def replay_run(
    workdir: Path,
    record: dict[str, Any],
    *,
    expected_label: str,
) -> dict[str, Any]:
    """Re-read raw output and verify every recorded digest and metric."""

    required = (
        ("script", "script_sha256"),
        ("stdout", "stdout_sha256"),
        ("stderr", "stderr_sha256"),
        ("returncode_file", "returncode_file_sha256"),
    )
    paths: dict[str, Path] = {}
    for path_key, digest_key in required:
        value = record.get(path_key)
        if (
            not isinstance(value, str)
            or Path(value).is_absolute()
            or ".." in Path(value).parts
        ):
            raise PVTCornerError(f"OpenSTA record path {path_key} is unsafe")
        path = (workdir / value).resolve()
        try:
            path.relative_to(workdir.resolve())
        except ValueError as exc:
            raise PVTCornerError(f"OpenSTA record path {path_key} escapes") from exc
        if not path.is_file() or path.is_symlink():
            raise PVTCornerError(f"OpenSTA record file is invalid: {path}")
        if sha256_file(path) != record.get(digest_key):
            raise PVTCornerError(f"OpenSTA record digest changed for {path_key}")
        paths[path_key] = path
    returncode_text = paths["returncode_file"].read_text(encoding="utf-8").strip()
    if returncode_text != "0" or record.get("returncode") != 0:
        raise PVTCornerError("OpenSTA replay record has a nonzero return code")
    metrics = parse_opensta_output(
        paths["stdout"].read_text(encoding="utf-8"),
        paths["stderr"].read_text(encoding="utf-8"),
        expected_label=expected_label,
    )
    if not metrics_equal(metrics, record.get("metrics", {})):
        raise PVTCornerError(
            "OpenSTA recorded metrics differ from the raw report replay"
        )
    return metrics
