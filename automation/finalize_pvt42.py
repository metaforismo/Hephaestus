from __future__ import annotations

import ast
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

REPOSITORY = os.environ["GITHUB_REPOSITORY"]
TOKEN = os.environ["GITHUB_TOKEN"]
PR_NUMBER = 42
AUTOMATION_BRANCH = "automation/finalize-pvt-42"
TRIGGER_PATH = "configs/evidence/pvt_ci_trigger.json"
WORK = Path("/tmp/hephaestus-pvt-finalizer")
PR_WORKTREE = WORK / "pr"
ARTIFACT_ROOT = WORK / "artifact"
EXPECTED_WORKFLOWS = {
    "CI",
    "Evidence",
    "Matched baselines",
    "Synthesis evidence",
    "Formal equivalence",
    "IHP mapped evidence",
    "IHP mapped formal equivalence",
    "IHP ABC area-delay evidence",
    "IHP ABC area-delay formal equivalence",
    "Pinned OpenSTA pre-layout timing evidence",
    "Registered matched tiles",
    "Pinned IHP OpenROAD physical evidence",
    "Repository hygiene",
    "Routed SPEF semantic evidence",
    "Routed IHP PVT corner evidence",
}
PVT_WORKFLOW = "Routed IHP PVT corner evidence"
BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
CORNERS = ("slow", "typ", "fast")


def fail(message: str) -> None:
    raise RuntimeError(message)


def require(condition: bool, message: str) -> None:
    if not condition:
        fail(message)


def api(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {TOKEN}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "hephaestus-pvt-finalizer",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"https://api.github.com{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            content = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        fail(f"GitHub API {method} {path} failed: {exc.code}: {detail}")
    if not content:
        return None
    return json.loads(content)


def run(
    *args: str,
    cwd: Path | None = None,
    check: bool = True,
    timeout: int = 1800,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )
    if check and completed.returncode != 0:
        fail(
            f"command failed ({completed.returncode}): {' '.join(args)}\n"
            f"stdout:\n{completed.stdout[-12000:]}\n"
            f"stderr:\n{completed.stderr[-12000:]}"
        )
    return completed


def comment(body: str) -> None:
    api(
        "POST",
        f"/repos/{REPOSITORY}/issues/{PR_NUMBER}/comments",
        {"body": body},
    )


def get_pr() -> dict[str, Any]:
    value = api("GET", f"/repos/{REPOSITORY}/pulls/{PR_NUMBER}")
    require(value["state"] == "open", "PVT PR is not open")
    require(value["head"]["repo"]["full_name"] == REPOSITORY, "PVT PR head is external")
    require(
        value["head"]["ref"] == "feat/qualified-ihp-pvt-evidence",
        "unexpected PVT PR branch",
    )
    return value


def checkout_pr(head_sha: str) -> None:
    shutil.rmtree(PR_WORKTREE, ignore_errors=True)
    run("git", "fetch", "origin", f"refs/pull/{PR_NUMBER}/head", cwd=Path.cwd())
    run("git", "worktree", "prune", cwd=Path.cwd())
    run(
        "git",
        "worktree",
        "add",
        "--detach",
        str(PR_WORKTREE),
        head_sha,
        cwd=Path.cwd(),
    )
    actual = run("git", "rev-parse", "HEAD", cwd=PR_WORKTREE).stdout.strip()
    require(actual == head_sha, "checked-out PR head differs")


def latest_runs(head_sha: str) -> dict[str, dict[str, Any]]:
    response = api(
        "GET",
        f"/repos/{REPOSITORY}/actions/runs?head_sha={head_sha}&per_page=100",
    )
    latest: dict[str, dict[str, Any]] = {}
    for value in response.get("workflow_runs", []):
        name = value["name"]
        previous = latest.get(name)
        if previous is None or value["run_number"] > previous["run_number"]:
            latest[name] = value
    return latest


def wait_for_workflow(head_sha: str, name: str, timeout_seconds: int = 18000) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = get_pr()["head"]["sha"]
        require(current == head_sha, f"PR head changed while waiting for {name}: {current}")
        value = latest_runs(head_sha).get(name)
        if value is None:
            time.sleep(30)
            continue
        if value["status"] == "completed":
            require(value.get("conclusion") == "success", f"{name} failed: {value['html_url']}")
            return value
        time.sleep(30)
    fail(f"timed out waiting for {name} on {head_sha}")


def wait_for_all_workflows(head_sha: str, timeout_seconds: int = 21600) -> dict[str, dict[str, Any]]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = get_pr()["head"]["sha"]
        require(current == head_sha, f"PR head changed while waiting for exact-head CI: {current}")
        values = latest_runs(head_sha)
        missing = EXPECTED_WORKFLOWS - set(values)
        active = [
            name
            for name in EXPECTED_WORKFLOWS
            if name in values and values[name]["status"] != "completed"
        ]
        failed = [
            name
            for name in EXPECTED_WORKFLOWS
            if name in values
            and values[name]["status"] == "completed"
            and values[name].get("conclusion") != "success"
        ]
        if failed:
            detail = ", ".join(f"{name}: {values[name]['html_url']}" for name in failed)
            fail(f"exact-head workflow failure(s): {detail}")
        if not missing and not active:
            return values
        time.sleep(30)
    fail(f"timed out waiting for exact-head workflows on {head_sha}")


def download_artifact(run_id: int, artifact_name: str, destination: Path) -> None:
    shutil.rmtree(destination, ignore_errors=True)
    destination.mkdir(parents=True)
    environment = dict(os.environ)
    environment["GH_TOKEN"] = TOKEN
    run(
        "gh",
        "run",
        "download",
        str(run_id),
        "--repo",
        REPOSITORY,
        "--name",
        artifact_name,
        "--dir",
        str(destination),
        env=environment,
        timeout=1200,
    )


def parse_report(directory: Path, record: dict[str, Any], label: str) -> dict[str, Any]:
    slack_re = re.compile(
        r"^\s*(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s+slack\s+"
        r"\((MET|VIOLATED)\)\s*$",
        re.MULTILINE,
    )
    tns_re = re.compile(
        r"^\s*tns\s+(-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*$",
        re.MULTILINE | re.IGNORECASE,
    )
    marker_re = re.compile(r"^HEPHAESTUS_PVT_CORNER=(\S+)$", re.MULTILINE)
    paths: dict[str, Path] = {}
    for path_key, digest_key in (
        ("script", "script_sha256"),
        ("stdout", "stdout_sha256"),
        ("stderr", "stderr_sha256"),
        ("returncode_file", "returncode_file_sha256"),
    ):
        relative = Path(record[path_key])
        require(not relative.is_absolute() and ".." not in relative.parts, "unsafe report path")
        candidate = directory / relative
        require(candidate.is_file() and not candidate.is_symlink(), f"invalid report file {candidate}")
        require(
            hashlib.sha256(candidate.read_bytes()).hexdigest() == record[digest_key],
            f"report digest mismatch for {candidate}",
        )
        paths[path_key] = candidate
    require(paths["returncode_file"].read_text().strip() == "0", "nonzero recorded return code")
    require(record["returncode"] == 0, "nonzero manifest return code")
    stdout = paths["stdout"].read_text(encoding="utf-8")
    stderr = paths["stderr"].read_text(encoding="utf-8")
    combined = stdout + "\n" + stderr
    require("HEPHAESTUS_PVT_DONE=1" in stdout, "missing completion marker")
    require(marker_re.findall(stdout) == [label], "corner marker differs")
    require(
        re.search(r"(?m)^\s*(?:Error:|%Error|FATAL:)", combined) is None,
        "fatal OpenSTA diagnostic present",
    )
    slacks = slack_re.findall(stdout)
    tns_values = tns_re.findall(stdout)
    require(bool(slacks) and bool(tns_values), "timing metrics missing")
    slack = float(slacks[-1][0])
    status = slacks[-1][1].lower()
    tns = float(tns_values[-1])
    require(math.isfinite(slack) and math.isfinite(tns), "non-finite timing metric")
    require(tns <= 0, "positive total negative slack")
    require(status == ("violated" if slack < 0 else "met"), "slack status mismatch")
    metrics = record["metrics"]
    require(metrics["slack_status"] == status, "recorded status differs")
    require(
        math.isclose(float(metrics["worst_setup_slack_ns"]), slack, rel_tol=0, abs_tol=1e-9),
        "recorded slack differs",
    )
    require(
        math.isclose(float(metrics["total_negative_slack_ns"]), tns, rel_tol=0, abs_tol=1e-9),
        "recorded TNS differs",
    )
    return metrics


def inspect_artifact(
    artifact_root: Path,
    *,
    head_sha: str,
    strict: bool,
    worktree: Path,
) -> dict[str, Any]:
    manifests = list(artifact_root.rglob("pvt_corner_evidence.json"))
    require(len(manifests) == 1, f"expected one PVT manifest, got {len(manifests)}")
    path = manifests[0]
    evidence = json.loads(path.read_text(encoding="utf-8"))
    require(evidence["schema"] == "hephaestus.ihp-pvt-corner-evidence.v2", "schema differs")
    require(
        evidence["evidence_level"] == "routed_spef_opensta_three_corner_characterization",
        "evidence level differs",
    )
    require(evidence["execution"]["source_revision"] == head_sha, "source revision differs")
    claims = evidence["claims"]
    for name in (
        "all_36_positive_analyses_completed",
        "analysis_replay_repeatability_verified",
        "physical_attempt_timing_repeatability_verified",
        "six_tight_clock_negative_controls_detected",
        "raw_report_replay_verified",
        "multi_corner_timing_observed",
    ):
        require(claims[name] is True, f"required PVT claim is false: {name}")
    for name in (
        "ocv_analyzed",
        "aocv_analyzed",
        "pocv_analyzed",
        "statistical_variation_analyzed",
        "crosstalk_delay_analyzed",
        "ir_drop_analyzed",
        "electromigration_analyzed",
        "thermal_analyzed",
        "foundry_signoff_sta_performed",
        "foundry_signoff_complete",
        "silicon_verified",
    ):
        require(claims[name] is False, f"overstated PVT claim: {name}")
    if strict:
        require(evidence["regression"]["passed"] is True, "strict regression did not pass")
        require(claims["comparative_pvt_claim_enabled"] is True, "comparative PVT claim disabled")
    else:
        require(evidence["regression"]["passed"] is False, "bootstrap unexpectedly passed regression")
        require(
            evidence["regression"]["bootstrap_reference_required"] is True,
            "bootstrap boundary missing",
        )
        require(claims["comparative_pvt_claim_enabled"] is False, "bootstrap overstates PVT")

    positives = 0
    controls = 0
    raw_files = 0
    for backend in BACKENDS:
        value = evidence["backends"][backend]
        require(value["physical_attempt_timing_repeatability_verified"] is True, "attempt drift")
        require(set(value["physical_attempts"]) == {"1", "2"}, "physical attempt set differs")
        for attempt in (1, 2):
            case = value["physical_attempts"][str(attempt)]
            require(set(case["corners"]) == set(CORNERS), "corner set differs")
            for corner in CORNERS:
                corner_value = case["corners"][corner]
                require(len(corner_value["replays"]) == 2, "analysis replay count differs")
                replay_metrics = []
                for replay in corner_value["replays"]:
                    directory = (
                        path.parent
                        / "backends"
                        / backend
                        / f"physical-attempt-{attempt}"
                        / "corners"
                        / corner
                        / f"replay-{replay['replay']}"
                    )
                    replay_metrics.append(parse_report(directory, replay, corner))
                    positives += 1
                    raw_files += 4
                require(replay_metrics[0] == replay_metrics[1], "analysis replay drift")
                require(corner_value["metrics"] == replay_metrics[0], "corner metrics differ")
            control = case["negative_control"]
            directory = (
                path.parent
                / "backends"
                / backend
                / f"physical-attempt-{attempt}"
                / "negative-control"
            )
            metrics = parse_report(directory, control["analysis"], "typ-tight-clock-control")
            require(control["timing_violation_observed"] is True, "control not detected")
            require(metrics["worst_setup_slack_ns"] < 0, "control slack is not negative")
            require(metrics["total_negative_slack_ns"] < 0, "control TNS is not negative")
            controls += 1
            raw_files += 4
    require(positives == 36, f"positive matrix differs: {positives}")
    require(controls == 6, f"negative-control matrix differs: {controls}")
    require(raw_files == 168, f"raw report-file count differs: {raw_files}")

    if strict:
        reference = worktree / "benchmarks/reference/ihp_sg13g2_pvt_corner_tiny_v2.json"
        require(reference.is_file(), "versioned PVT reference is missing")
        run(
            sys.executable,
            "-m",
            "hephaestus.pvt_corner",
            "validate-reference",
            str(path),
            str(reference),
            cwd=worktree,
            timeout=600,
        )
    return {
        "manifest_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "positive_analyses": positives,
        "negative_controls": controls,
        "raw_report_files_replayed": raw_files,
        "strict": strict,
    }


def upsert_after_title(path: Path, marker: str, body: str) -> bool:
    text = path.read_text(encoding="utf-8")
    start = f"<!-- {marker}_START -->"
    end = f"<!-- {marker}_END -->"
    block = f"{start}\n{body.strip()}\n{end}"
    if start in text or end in text:
        require(text.count(start) == 1 and text.count(end) == 1, f"malformed marker in {path}")
        begin = text.index(start)
        finish = text.index(end, begin) + len(end)
        updated = text[:begin] + block + text[finish:]
    else:
        lines = text.splitlines()
        heading = next((index for index, line in enumerate(lines) if line.startswith("#")), None)
        require(heading is not None, f"Markdown title missing in {path}")
        insert = heading + 1
        while insert < len(lines) and not lines[insert].strip():
            insert += 1
        lines[insert:insert] = ["", block, ""]
        updated = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    if updated == text:
        return False
    path.write_text(updated, encoding="utf-8")
    return True


def reconcile_docs(worktree: Path) -> bool:
    scope = (
        "The permanent routed PVT gate covers the registered 4×6 IHP SG13G2 "
        "regression microcase: three matched backends, two independently generated "
        "physical attempts, slow/typical/fast official Liberty views, and two isolated "
        "OpenSTA replays per case (36 positive analyses), plus six tight-clock controls."
    )
    boundary = (
        "It enables `comparative_pvt_claim_enabled` only when the versioned regression "
        "reference passes on the exact head. It does **not** establish OCV/AOCV/POCV, "
        "statistical variation, crosstalk delay, IR drop, electromigration, thermal or "
        "aging behavior, foundry-signoff STA, foundry sign-off, or silicon behavior."
    )
    blocks = {
        "README.md": (
            "HEPHAESTUS_PVT_STATUS_V2",
            f"## Qualified routed PVT evidence\n\n{scope}\n\n{boundary}\n\n"
            "Method, provenance, controls, and reproduction details are in "
            "[`docs/PVT_CORNER_EVIDENCE.md`](docs/PVT_CORNER_EVIDENCE.md).",
        ),
        "docs/ROADMAP.md": (
            "HEPHAESTUS_PVT_ROADMAP_V2",
            f"## Routed PVT milestone\n\n- [x] {scope}\n- [x] Exact Liberty, PDK, "
            "routed Verilog, SPEF, SDC, OpenSTA, and raw-report provenance.\n"
            "- [x] Six backend/attempt-specific tight-clock controls.\n"
            "- [ ] OCV/AOCV/POCV, crosstalk, IR/EM, thermal, aging, and sign-off.\n\n"
            f"{boundary}",
        ),
        "docs/POST_PHYSICAL_STATUS.md": (
            "HEPHAESTUS_PVT_POST_PHYSICAL_V2",
            f"## Current routed PVT status\n\n{scope}\n\n```json\n{{\n"
            '  "multi_corner_timing_observed": true,\n'
            '  "comparative_pvt_claim_enabled": true,\n'
            '  "ocv_analyzed": false,\n'
            '  "aocv_analyzed": false,\n'
            '  "pocv_analyzed": false,\n'
            '  "statistical_variation_analyzed": false,\n'
            '  "crosstalk_delay_analyzed": false,\n'
            '  "foundry_signoff_sta_performed": false,\n'
            '  "foundry_signoff_complete": false,\n'
            '  "silicon_verified": false\n'
            "}\n```\n\n"
            f"{boundary}",
        ),
        "docs/OPENROAD_PHYSICAL_EVIDENCE.md": (
            "HEPHAESTUS_PVT_DOWNSTREAM_V2",
            f"## Downstream routed PVT qualification\n\n{scope}\n\nThe timing layer "
            "consumes the exact routed Verilog, SPEF, SDC, and run manifests from both "
            "physical attempts for every backend. Physical repeatability is therefore a "
            "prerequisite, not a substitute for timing analysis.\n\n"
            f"{boundary}",
        ),
    }
    changed = False
    for relative, (marker, body) in blocks.items():
        changed = upsert_after_title(worktree / relative, marker, body) or changed
    return changed


def install_and_test(worktree: Path, *, full: bool) -> None:
    run(sys.executable, "-m", "pip", "install", "-e", f"{worktree}[dev]", timeout=900)
    run("ruff", "check", "src/hephaestus/pvt_corner", "tests/test_pvt_corner.py", cwd=worktree)
    run("ruff", "format", "--check", "src/hephaestus/pvt_corner", "tests/test_pvt_corner.py", cwd=worktree)
    run("pytest", "-q", "tests/test_pvt_corner.py", cwd=worktree, timeout=1200)
    run(sys.executable, "scripts/check_repo_hygiene.py", cwd=worktree)
    run("git", "diff", "--check", cwd=worktree)
    if full:
        run("ruff", "check", ".", cwd=worktree, timeout=1200)
        run("ruff", "format", "--check", ".", cwd=worktree, timeout=1200)
        run("pytest", "-q", cwd=worktree, timeout=3600)


def commit_regular_changes(worktree: Path, message: str) -> str:
    run("git", "config", "user.name", "github-actions[bot]", cwd=worktree)
    run(
        "git",
        "config",
        "user.email",
        "41898282+github-actions[bot]@users.noreply.github.com",
        cwd=worktree,
    )
    run("git", "add", "-A", cwd=worktree)
    run("git", "diff", "--cached", "--check", cwd=worktree)
    names = run("git", "diff", "--cached", "--name-only", cwd=worktree).stdout.splitlines()
    require(names, "promotion produced no regular-file changes")
    require(
        all(not name.startswith(".github/workflows/") for name in names),
        "promotion attempted to modify a workflow file",
    )
    run("git", "commit", "-m", message, cwd=worktree)
    head = run("git", "rev-parse", "HEAD", cwd=worktree).stdout.strip()
    run(
        "git",
        "push",
        "origin",
        f"HEAD:feat/qualified-ihp-pvt-evidence",
        cwd=worktree,
        timeout=900,
    )
    return head


def promote_reference_and_docs() -> str:
    pr = get_pr()
    head = pr["head"]["sha"]
    checkout_pr(head)
    temp_workflows = [
        path.relative_to(PR_WORKTREE).as_posix()
        for path in (PR_WORKTREE / ".github/workflows").glob("*.yml")
        if any(
            token in path.name
            for token in (
                "bootstrap-",
                "research-",
                "one-shot-",
                "assemble-",
                "promote-",
                "finalize-",
                "finish-",
            )
        )
    ]
    require(not temp_workflows, f"temporary workflow residue remains: {temp_workflows}")
    reference = PR_WORKTREE / "benchmarks/reference/ihp_sg13g2_pvt_corner_tiny_v2.json"
    changes = False
    bootstrap_inspection: dict[str, Any] | None = None
    if not reference.is_file():
        run_value = wait_for_workflow(head, PVT_WORKFLOW)
        download_artifact(
            run_value["id"],
            "hephaestus-ihp-pvt-corner-bootstrap",
            ARTIFACT_ROOT,
        )
        install_and_test(PR_WORKTREE, full=False)
        bootstrap_inspection = inspect_artifact(
            ARTIFACT_ROOT,
            head_sha=head,
            strict=False,
            worktree=PR_WORKTREE,
        )
        manifest = next(ARTIFACT_ROOT.rglob("pvt_corner_evidence.json"))
        reference.parent.mkdir(parents=True, exist_ok=True)
        run(
            sys.executable,
            "-m",
            "hephaestus.pvt_corner",
            "reference",
            str(manifest),
            "--out",
            str(reference),
            cwd=PR_WORKTREE,
            timeout=600,
        )
        run(
            sys.executable,
            "-m",
            "hephaestus.pvt_corner",
            "validate-reference",
            str(manifest),
            str(reference),
            cwd=PR_WORKTREE,
            timeout=600,
        )
        changes = True
    changes = reconcile_docs(PR_WORKTREE) or changes
    install_and_test(PR_WORKTREE, full=False)
    if not changes:
        return head
    promoted = commit_regular_changes(
        PR_WORKTREE,
        "docs: promote inspected routed PVT reference and status",
    )
    detail = ""
    if bootstrap_inspection is not None:
        detail = (
            f"\n\nBootstrap inspection: `{json.dumps(bootstrap_inspection, sort_keys=True)}`"
        )
    comment(
        "PVT bootstrap/reference promotion completed.\n\n"
        f"Promoted head: `{promoted}`. The commit contains only the inspected reference "
        "and permanent documentation updates. A user-authored trigger commit is now "
        f"required so all exact-head workflows execute on the promoted content.{detail}"
    )
    return promoted


def wait_for_external_head_change(previous: str, purpose: str, timeout_seconds: int = 10800) -> str:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        current = get_pr()["head"]["sha"]
        if current != previous:
            return current
        time.sleep(30)
    fail(f"timed out waiting for external {purpose} commit after {previous}")


def changed_files(base: str, head: str) -> list[str]:
    comparison = api("GET", f"/repos/{REPOSITORY}/compare/{base}...{head}")
    return [value["filename"] for value in comparison.get("files", [])]


def security_and_diff_audit(worktree: Path, base_sha: str, head_sha: str) -> None:
    files = changed_files(base_sha, head_sha)
    allowed_exact = {
        ".github/workflows/pvt-corner-evidence.yml",
        "configs/evidence/ihp_sg13g2_pvt_corner_v2.json",
        "benchmarks/reference/ihp_sg13g2_pvt_corner_tiny_v2.json",
        "docs/PVT_CORNER_EVIDENCE.md",
        "tests/test_pvt_corner.py",
        "README.md",
        "docs/ROADMAP.md",
        "docs/POST_PHYSICAL_STATUS.md",
        "docs/OPENROAD_PHYSICAL_EVIDENCE.md",
    }
    unexpected = [
        name
        for name in files
        if name not in allowed_exact and not name.startswith("src/hephaestus/pvt_corner/")
    ]
    require(not unexpected, f"unexpected PVT diff paths: {unexpected}")
    require(TRIGGER_PATH not in files, "temporary exact-head trigger remains in final diff")
    for name in files:
        require(
            not any(
                token in name
                for token in (
                    "bootstrap",
                    "payload",
                    "research-",
                    "one-shot",
                    "assemble",
                    "promote",
                    "finalize",
                    "finish",
                )
            ),
            f"temporary residue in final diff: {name}",
        )
    workflow = (worktree / ".github/workflows/pvt-corner-evidence.yml").read_text(
        encoding="utf-8"
    )
    require("pull_request_target" not in workflow, "unsafe pull_request_target trigger")
    require("contents: write" not in workflow, "permanent PVT workflow has write permission")
    require("pull-requests: write" not in workflow, "permanent PVT workflow can write PRs")
    require("actions: read" in workflow and "contents: read" in workflow, "read boundary missing")
    for name in files:
        if not name.endswith(".py"):
            continue
        tree = ast.parse((worktree / name).read_text(encoding="utf-8"), filename=name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                    fail(f"dynamic execution introduced in {name}:{node.lineno}")
                if (
                    isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "os"
                    and node.func.attr == "system"
                ):
                    fail(f"os.system introduced in {name}:{node.lineno}")
                if any(
                    keyword.arg == "shell"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                    for keyword in node.keywords
                ):
                    fail(f"shell=True introduced in {name}:{node.lineno}")
    run("git", "diff", "--check", f"{base_sha}...{head_sha}", cwd=worktree)


def verify_strict_head(head_sha: str) -> dict[str, Any]:
    values = wait_for_all_workflows(head_sha)
    checkout_pr(head_sha)
    reference = PR_WORKTREE / "benchmarks/reference/ihp_sg13g2_pvt_corner_tiny_v2.json"
    require(reference.is_file(), "strict head lacks PVT reference")
    install_and_test(PR_WORKTREE, full=True)
    run_value = values[PVT_WORKFLOW]
    download_artifact(
        run_value["id"],
        "hephaestus-ihp-pvt-corner-evidence",
        ARTIFACT_ROOT,
    )
    inspection = inspect_artifact(
        ARTIFACT_ROOT,
        head_sha=head_sha,
        strict=True,
        worktree=PR_WORKTREE,
    )
    return {
        "workflow_run_id": run_value["id"],
        "workflow_url": run_value["html_url"],
        "inspection": inspection,
    }


def merge_and_verify_main(head_sha: str, strict: dict[str, Any]) -> str:
    pr = get_pr()
    base_sha = pr["base"]["sha"]
    checkout_pr(head_sha)
    security_and_diff_audit(PR_WORKTREE, base_sha, head_sha)
    environment = dict(os.environ)
    environment["GH_TOKEN"] = TOKEN
    run("gh", "pr", "ready", str(PR_NUMBER), "--repo", REPOSITORY, env=environment)
    run(
        "gh",
        "pr",
        "merge",
        str(PR_NUMBER),
        "--repo",
        REPOSITORY,
        "--squash",
        "--match-head-commit",
        head_sha,
        "--subject",
        "Add qualified routed IHP PVT corner evidence (#42)",
        "--body",
        "Qualify exact routed slow/typical/fast OpenSTA evidence for all three matched "
        "backends and both physical attempts. Bind official Liberty views, PDK and tool "
        "provenance, require 36 positive analyses, six tight-clock controls, raw report "
        "replay, a versioned regression reference, and exact-head independent inspection. "
        "Keep OCV/AOCV/POCV, crosstalk, IR/EM, thermal, sign-off, and silicon claims false.",
        env=environment,
        timeout=900,
    )
    deadline = time.time() + 600
    merged: dict[str, Any] | None = None
    while time.time() < deadline:
        value = api("GET", f"/repos/{REPOSITORY}/pulls/{PR_NUMBER}")
        if value.get("merged") is True:
            merged = value
            break
        time.sleep(10)
    require(merged is not None, "PR did not report a merged state")
    merge_sha = merged["merge_commit_sha"]

    deadline = time.time() + 21600
    while time.time() < deadline:
        branch = api("GET", f"/repos/{REPOSITORY}/branches/main")
        if branch["commit"]["sha"] != merge_sha:
            time.sleep(20)
            continue
        values = latest_runs(merge_sha)
        missing = EXPECTED_WORKFLOWS - set(values)
        failed = [
            name
            for name in EXPECTED_WORKFLOWS
            if name in values
            and values[name]["status"] == "completed"
            and values[name].get("conclusion") != "success"
        ]
        if failed:
            fail(f"main workflow failure after merge: {failed}")
        active = [
            name
            for name in EXPECTED_WORKFLOWS
            if name in values and values[name]["status"] != "completed"
        ]
        if not missing and not active:
            break
        time.sleep(30)
    else:
        fail("timed out waiting for main exact-head workflows")

    shutil.rmtree(PR_WORKTREE, ignore_errors=True)
    run("git", "fetch", "origin", "main", cwd=Path.cwd())
    run("git", "worktree", "add", "--detach", str(PR_WORKTREE), merge_sha, cwd=Path.cwd())
    install_and_test(PR_WORKTREE, full=False)
    pvt_run = latest_runs(merge_sha)[PVT_WORKFLOW]
    download_artifact(
        pvt_run["id"],
        "hephaestus-ihp-pvt-corner-evidence",
        ARTIFACT_ROOT,
    )
    main_inspection = inspect_artifact(
        ARTIFACT_ROOT,
        head_sha=merge_sha,
        strict=True,
        worktree=PR_WORKTREE,
    )
    comment(
        "PVT qualification merged and reconstructed on `main`.\n\n"
        f"- PR head: `{head_sha}`\n"
        f"- merge commit: `{merge_sha}`\n"
        f"- PR strict artifact: `{json.dumps(strict, sort_keys=True)}`\n"
        f"- main strict artifact: `{json.dumps(main_inspection, sort_keys=True)}`\n\n"
        "The qualified scope remains the exact registered 4×6 IHP SG13G2 regression "
        "microcase. OCV/AOCV/POCV, crosstalk, IR/EM, thermal, foundry sign-off, and "
        "silicon claims remain false."
    )
    return merge_sha


def close_superseded_and_cleanup(merge_sha: str) -> None:
    for number, branch in (
        (38, "research/ihp-pvt-corner-probe"),
        (39, "feat/ihp-pvt-corner-evidence"),
    ):
        value = api("GET", f"/repos/{REPOSITORY}/pulls/{number}")
        if value["state"] == "open":
            api(
                "POST",
                f"/repos/{REPOSITORY}/issues/{number}/comments",
                {
                    "body": (
                        f"Superseded by #42, merged as `{merge_sha}` after exact-head "
                        "bootstrap/reference qualification, strict artifact inspection, "
                        "and reconstruction on `main`. This research/legacy implementation "
                        "is intentionally not merged."
                    )
                },
            )
            api("PATCH", f"/repos/{REPOSITORY}/pulls/{number}", {"state": "closed"})
        try:
            api(
                "DELETE",
                f"/repos/{REPOSITORY}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
            )
        except RuntimeError:
            pass
    for branch in (
        "feat/ihp-pvt-corner-evidence-v2",
        "feat/qualified-ihp-pvt-evidence",
    ):
        try:
            api(
                "DELETE",
                f"/repos/{REPOSITORY}/git/refs/heads/{urllib.parse.quote(branch, safe='')}",
            )
        except RuntimeError:
            pass


def main() -> int:
    WORK.mkdir(parents=True, exist_ok=True)
    try:
        promoted = promote_reference_and_docs()
        current = get_pr()["head"]["sha"]
        if current == promoted:
            comment(
                f"PVT promotion head `{promoted}` is ready. Waiting for the external "
                f"exact-head trigger `{TRIGGER_PATH}`."
            )
            trigger_head = wait_for_external_head_change(promoted, "strict-CI trigger")
        else:
            trigger_head = current
        checkout_pr(trigger_head)
        require((PR_WORKTREE / TRIGGER_PATH).is_file(), "strict-CI trigger file is missing")
        trigger_verification = verify_strict_head(trigger_head)
        comment(
            "The strict trigger head passed all permanent workflows and independent "
            f"artifact replay: `{json.dumps(trigger_verification, sort_keys=True)}`. "
            "Waiting for external removal of the temporary trigger before the final head."
        )
        final_head = wait_for_external_head_change(trigger_head, "trigger-removal")
        checkout_pr(final_head)
        require(not (PR_WORKTREE / TRIGGER_PATH).exists(), "temporary trigger remains")
        final_verification = verify_strict_head(final_head)
        merge_sha = merge_and_verify_main(final_head, final_verification)
        close_superseded_and_cleanup(merge_sha)
        try:
            api(
                "DELETE",
                f"/repos/{REPOSITORY}/git/refs/heads/"
                f"{urllib.parse.quote(AUTOMATION_BRANCH, safe='')}",
            )
        except RuntimeError:
            pass
        return 0
    except Exception as exc:
        try:
            comment(
                "PVT finalization stopped fail-closed. No merge was attempted after the "
                f"failing boundary.\n\n```
{type(exc).__name__}: {exc}
```"
            )
        except Exception:
            pass
        raise


if __name__ == "__main__":
    raise SystemExit(main())
