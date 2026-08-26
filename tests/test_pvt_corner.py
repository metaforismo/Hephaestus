from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from hephaestus import pvt_corner
from hephaestus.pvt_corner import _common, _opensta, _reference

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")
CORNERS = ("slow", "typ", "fast")
REVISION = "1" * 40
UPSTREAM_RUN_ID = "123"
UPSTREAM_WORKFLOW_REF = (
    "metaforismo/Hephaestus/.github/workflows/"
    "openroad-physical-evidence.yml@refs/pull/42/merge"
)


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _sha(path: Path) -> str:
    return _common.sha256_file(path)


def _physical_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "pinned_orfs_image_used": True,
        "all_three_backends_placed": True,
        "all_three_backends_routed": True,
        "all_three_backends_emitted_gds": True,
        "all_three_backends_emitted_spef": True,
        "two_attempts_per_backend_completed": True,
        "physical_repeatability_verified": True,
        "physical_metrics_recorded": True,
        "common_physical_boundary_verified": True,
        "post_physical_equivalence_verified": False,
        "comparative_ppa_claim_enabled": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _post_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "both_physical_attempts_per_backend_bound": True,
        "all_three_routed_registered_implementations_equivalent": True,
        "data_corruption_negative_control_detected": True,
        "valid_latency_negative_control_detected": True,
        "reset_state_negative_control_detected": True,
        "post_physical_equivalence_verified": True,
        "comparative_ppa_claim_enabled": True,
        "four_state_semantics_verified": False,
        "timing_annotated_functional_semantics_verified": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _run_claims() -> dict[str, bool]:
    return {
        "registered_source_binding_verified": True,
        "pinned_orfs_image_used": True,
        "placement_performed": True,
        "routing_performed": True,
        "gds_generated": True,
        "spef_generated": True,
        "metadata_generated": True,
        "post_physical_equivalence_verified": False,
        "drc_clean": False,
        "lvs_clean": False,
        "power_estimated_with_activity": False,
        "post_layout_pex_verified": False,
        "foundry_signoff_complete": False,
        "silicon_verified": False,
    }


def _make_pdk(tmp_path: Path) -> tuple[Path, dict[str, object]]:
    root = tmp_path / "pdk"
    lib_root = root / "libs"
    lib_root.mkdir(parents=True)
    paths: dict[str, Path] = {}
    for label in CORNERS:
        path = lib_root / f"{label}.lib"
        path.write_text(f"library({label}) {{}}\n", encoding="utf-8")
        paths[label] = path
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.name", "fixture"],
        cwd=root,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        text=True,
    ).strip()
    liberty: dict[str, object] = {}
    conditions = {
        "slow": (1.08, 125.0),
        "typ": (1.2, 25.0),
        "fast": (1.32, -40.0),
    }
    for label, path in paths.items():
        blob = subprocess.check_output(
            ["git", "hash-object", str(path.relative_to(root))],
            cwd=root,
            text=True,
        ).strip()
        voltage, temperature = conditions[label]
        liberty[label] = {
            "path": path.relative_to(root).as_posix(),
            "sha256": _sha(path),
            "git_blob_sha": blob,
            "nominal_voltage_v": voltage,
            "nominal_temperature_c": temperature,
        }
    return root, {"commit": commit, "liberty": liberty}


def _make_opensta(tmp_path: Path, commit: str) -> tuple[Path, Path]:
    executable = tmp_path / "opensta.bin"
    executable.write_text(
        """#!/bin/sh
set -eu
label=$(grep -o 'HEPHAESTUS_PVT_CORNER=[a-z0-9_-]*' "$1" | head -n 1 | cut -d= -f2)
case "$label" in
  slow) slack=0.25; status=MET; tns=0.0 ;;
  typ) slack=0.5; status=MET; tns=0.0 ;;
  fast) slack=0.75; status=MET; tns=0.0 ;;
  *) slack=-1.25; status=VIOLATED; tns=-2.5 ;;
esac
printf 'HEPHAESTUS_PVT_REPORT_SCHEMA=2\n'
printf 'HEPHAESTUS_PVT_CLOCK_COUNT=1\n'
printf 'HEPHAESTUS_PVT_CHECK_SETUP_OK=1\n'
printf 'HEPHAESTUS_PVT_PATH_COUNT=1\n'
printf 'Found 0 unannotated drivers.\n'
printf 'Found 0 partially unannotated drivers.\n'
printf 'HEPHAESTUS_PVT_CORNER=%s\n' "$label"
printf '%s slack (%s)\n' "$slack" "$status"
printf 'worst slack max %s\n' "$slack"
printf 'tns max %s\n' "$tns"
printf 'HEPHAESTUS_PVT_DONE=1\n'
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    manifest = tmp_path / "opensta-tool.json"
    _write_json(
        manifest,
        {
            "schema": "hephaestus.opensta-tool.v1",
            "repository": "parallaxsw/OpenSTA",
            "commit": commit,
            "banner": "OpenSTA fixture",
            "binary": executable.name,
            "binary_sha256": _sha(executable),
            "binary_reproducibility_verified": False,
            "cudd": {"url": "fixture", "sha256": "0" * 64, "bytes": 1},
            "flex_header_sha256": "1" * 64,
            "packages_sha256": "2" * 64,
            "dynamic_libraries_sha256": "3" * 64,
        },
    )
    return executable, manifest


def _make_contract(
    tmp_path: Path,
    pdk_value: dict[str, object],
    opensta_commit: str,
) -> Path:
    path = tmp_path / "contract.json"
    _write_json(
        path,
        {
            "schema": "hephaestus.ihp-pvt-corner-contract.v2",
            "contract_id": "ihp-sg13g2-routed-pvt-corner-v2",
            "backends": list(BACKENDS),
            "corner_order": list(CORNERS),
            "physical_attempts": [1, 2],
            "analysis_replays": [1, 2],
            "timeout_seconds": 30,
            "negative_control_clock_period_ns": 0.05,
            "ihp_open_pdk": {
                "repository": "https://example.invalid/pdk.git",
                **pdk_value,
            },
            "opensta": {
                "repository": "parallaxsw/OpenSTA",
                "commit": opensta_commit,
            },
            "claim_boundary": {
                "ocv_analyzed": False,
                "aocv_analyzed": False,
                "pocv_analyzed": False,
                "statistical_variation_analyzed": False,
                "crosstalk_delay_analyzed": False,
                "ir_drop_analyzed": False,
                "electromigration_analyzed": False,
                "thermal_analyzed": False,
                "foundry_signoff_sta_performed": False,
                "foundry_signoff_complete": False,
                "silicon_verified": False,
            },
        },
    )
    return path


def _make_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    physical = tmp_path / "physical"
    post = tmp_path / "post"
    prepared_backends: dict[str, object] = {}
    physical_backends: dict[str, object] = {}
    post_backends: dict[str, object] = {}

    for backend in BACKENDS:
        design = physical / "prepared" / "designs" / backend
        design.mkdir(parents=True, exist_ok=True)
        sdc = design / "constraint.sdc"
        sdc.write_text(
            "create_clock -name core_clock -period 4.0 [get_ports clk]\n",
            encoding="utf-8",
        )
        prepared_backends[backend] = {
            "wrapper_module": f"top_{backend}",
            "sdc": f"designs/{backend}/constraint.sdc",
            "sdc_sha256": _sha(sdc),
        }
        runs: list[dict[str, object]] = []
        post_attempts: list[dict[str, object]] = []
        for attempt in (1, 2):
            run_root = (
                physical
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            )
            results = run_root / "results"
            results.mkdir(parents=True, exist_ok=True)
            netlist = results / "6_final.v"
            spef = results / "6_final.spef"
            netlist.write_text(
                f"module top_{backend}(input clk); endmodule\n",
                encoding="utf-8",
            )
            spef.write_text(f"SPEF {backend} stable\n", encoding="utf-8")
            manifest = {
                "schema": "hephaestus.openroad-physical-run.v1",
                "identity": {
                    "backend": backend,
                    "attempt": attempt,
                    "variant": f"attempt-{attempt:02d}",
                },
                "source": {},
                "artifacts": {
                    "final_verilog": {
                        "path": "results/6_final.v",
                        "sha256": _sha(netlist),
                        "size_bytes": netlist.stat().st_size,
                    },
                    "final_spef": {
                        "path": "results/6_final.spef",
                        "sha256": _sha(spef),
                        "size_bytes": spef.stat().st_size,
                    },
                },
                "normalized": {
                    "spef_date_normalized_sha256": _sha(spef),
                },
                "claims": _run_claims(),
            }
            original = run_root / "openroad_run.json"
            _write_json(original, manifest)
            digest = _sha(original)
            relative = f"run_manifests/{backend}-attempt-{attempt:02d}.json"
            bound = physical / "evidence" / relative
            bound.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, bound)
            runs.append(
                {
                    "attempt": attempt,
                    "manifest": relative,
                    "manifest_sha256": digest,
                    "artifacts": manifest["artifacts"],
                    "normalized": manifest["normalized"],
                }
            )
            post_attempts.append(
                {
                    "attempt": attempt,
                    "physical_run_manifest": {"sha256": digest},
                    "routed_verilog": {"sha256": _sha(netlist)},
                }
            )
        physical_backends[backend] = {
            "repeatability": {"passed": True},
            "runs": runs,
        }
        post_backends[backend] = {
            "passed": True,
            "both_physical_attempts_bound": True,
            "attempts": post_attempts,
        }

    prepared = physical / "prepared" / "prepared.json"
    _write_json(
        prepared,
        {
            "schema": "hephaestus.openroad-physical-prepared.v1",
            "backends": prepared_backends,
        },
    )
    prepared_digest = _sha(prepared)
    prepared_copy = physical / "evidence" / "source_prepared.json"
    prepared_copy.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(prepared, prepared_copy)

    physical_manifest = physical / "evidence" / "openroad_physical_evidence.json"
    _write_json(
        physical_manifest,
        {
            "schema": "hephaestus.openroad-physical-evidence.v1",
            "evidence_level": "matched_registered_orfs_rtl_to_gds_repeatability",
            "source": {"prepared_manifest_sha256": prepared_digest},
            "claims": _physical_claims(),
            "backends": physical_backends,
        },
    )
    physical_digest = _sha(physical_manifest)
    post_source = post / "source" / "openroad_physical_evidence.json"
    post_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(physical_manifest, post_source)
    post_manifest = post / "post_physical_equivalence_evidence.json"
    _write_json(
        post_manifest,
        {
            "schema": "hephaestus.post-physical-equivalence-evidence.v1",
            "evidence_level": (
                "exact_registered_source_to_routed_sequential_equivalence"
            ),
            "execution": {
                "source_revision": REVISION,
                "github_run_id": UPSTREAM_RUN_ID,
                "github_workflow_ref": UPSTREAM_WORKFLOW_REF,
            },
            "source": {
                "physical_evidence_sha256": physical_digest,
                "prepared_manifest_sha256": prepared_digest,
            },
            "claims": _post_claims(),
            "regression": {"passed": True},
            "backends": post_backends,
        },
    )
    return physical, post


def _fixture(tmp_path: Path) -> dict[str, Path]:
    pdk, pdk_value = _make_pdk(tmp_path)
    opensta_commit = "a" * 40
    executable, tool_manifest = _make_opensta(tmp_path, opensta_commit)
    contract = _make_contract(tmp_path, pdk_value, opensta_commit)
    physical, post = _make_artifacts(tmp_path)
    return {
        "physical": physical,
        "post": post,
        "pdk": pdk,
        "opensta": executable,
        "tool_manifest": tool_manifest,
        "contract": contract,
    }


def _valid_report(
    *,
    label: str = "slow",
    slack: str = "0.25",
    status: str = "MET",
    tns: str = "0.0",
) -> str:
    return "\n".join(
        [
            "HEPHAESTUS_PVT_REPORT_SCHEMA=2",
            "HEPHAESTUS_PVT_CLOCK_COUNT=1",
            "HEPHAESTUS_PVT_CHECK_SETUP_OK=1",
            "HEPHAESTUS_PVT_PATH_COUNT=1",
            "Found 0 unannotated drivers.",
            "Found 0 partially unannotated drivers.",
            f"HEPHAESTUS_PVT_CORNER={label}",
            f"{slack} slack ({status})",
            f"worst slack max {slack}",
            f"tns max {tns}",
            "HEPHAESTUS_PVT_DONE=1",
            "",
        ]
    )


def test_contract_rejects_an_overstated_signoff_claim(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    contract = json.loads(values["contract"].read_text(encoding="utf-8"))
    contract["claim_boundary"]["foundry_signoff_complete"] = True
    _write_json(values["contract"], contract)

    with pytest.raises(pvt_corner.PVTCornerError, match="claim boundary"):
        pvt_corner.validate_contract(values["contract"])


def test_contract_distinguishes_git_sha_from_sha256(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    contract = json.loads(values["contract"].read_text(encoding="utf-8"))
    contract["ihp_open_pdk"]["commit"] = "f" * 64
    _write_json(values["contract"], contract)

    with pytest.raises(pvt_corner.PVTCornerError, match="40-character Git SHA"):
        pvt_corner.validate_contract(values["contract"])


def test_tighten_sdc_requires_exactly_one_clock() -> None:
    assert "-period 0.05" in pvt_corner.tighten_sdc(
        "create_clock -period 4.0 [get_ports clk]\n",
        0.05,
    )
    with pytest.raises(pvt_corner.PVTCornerError, match="exactly one"):
        pvt_corner.tighten_sdc(
            "create_clock -period 4.0 [get_ports a]\n"
            "create_clock -period 5.0 [get_ports b]\n",
            0.05,
        )


def test_parse_opensta_output_requires_the_complete_report_contract() -> None:
    value = pvt_corner.parse_opensta_output(
        _valid_report(),
        "",
        expected_label="slow",
    )
    assert value["worst_setup_slack_ns"] == 0.25
    assert value["check_setup_passed"] is True
    assert value["clock_count"] == 1
    assert value["timing_path_count"] == 1
    assert value["unannotated_driver_count"] == 0

    with pytest.raises(pvt_corner.PVTCornerError, match="check_setup"):
        pvt_corner.parse_opensta_output(
            _valid_report().replace(
                "HEPHAESTUS_PVT_CHECK_SETUP_OK=1",
                "HEPHAESTUS_PVT_CHECK_SETUP_OK=0",
            ),
            "",
            expected_label="slow",
        )


def test_parse_opensta_output_rejects_fatal_diagnostics() -> None:
    with pytest.raises(pvt_corner.PVTCornerError, match="fatal diagnostic"):
        pvt_corner.parse_opensta_output(
            _valid_report(label="typ", slack="0.5"),
            "Error: read_spef failed\n",
            expected_label="typ",
        )


def test_end_to_end_bootstrap_reference_and_strict_qualification(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    bootstrap_out = tmp_path / "bootstrap"
    bootstrap = pvt_corner.build_evidence(
        values["physical"],
        values["post"],
        values["pdk"],
        values["opensta"],
        values["tool_manifest"],
        values["contract"],
        bootstrap_out,
        source_revision=REVISION,
        upstream_run_id=UPSTREAM_RUN_ID,
    )
    assert bootstrap["claims"]["all_36_positive_analyses_completed"] is True
    assert bootstrap["claims"]["comparative_pvt_claim_enabled"] is False
    assert bootstrap["regression"]["bootstrap_reference_required"] is True
    assert (
        sum(
            len(case["corners"][corner]["replays"])
            for backend in bootstrap["backends"].values()
            for case in backend["physical_attempts"].values()
            for corner in CORNERS
        )
        == 36
    )

    reference = tmp_path / "reference.json"
    pvt_corner.build_reference(
        bootstrap_out / "pvt_corner_evidence.json",
        reference,
    )
    final_out = tmp_path / "final"
    final = pvt_corner.build_evidence(
        values["physical"],
        values["post"],
        values["pdk"],
        values["opensta"],
        values["tool_manifest"],
        values["contract"],
        final_out,
        source_revision=REVISION,
        reference_path=reference,
        upstream_run_id=UPSTREAM_RUN_ID,
    )
    assert final["regression"]["passed"] is True
    assert final["claims"]["comparative_pvt_claim_enabled"] is True
    assert final["claims"]["foundry_signoff_sta_performed"] is False
    assert final["execution"]["upstream_physical_workflow_run_id"] == (
        UPSTREAM_RUN_ID
    )
    assert (
        pvt_corner.validate_existing_reference(
            final_out / "pvt_corner_evidence.json",
            reference,
        )["passed"]
        is True
    )


def test_reference_rejects_metric_drift(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    output = tmp_path / "bootstrap"
    pvt_corner.build_evidence(
        values["physical"],
        values["post"],
        values["pdk"],
        values["opensta"],
        values["tool_manifest"],
        values["contract"],
        output,
        source_revision=REVISION,
    )
    evidence_path = output / "pvt_corner_evidence.json"
    reference_path = tmp_path / "reference.json"
    pvt_corner.build_reference(evidence_path, reference_path)
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["backends"]["shared_dag"]["physical_attempts"]["1"]["corners"][
        "slow"
    ]["metrics"]["worst_setup_slack_ns"] = -99.0
    drifted = tmp_path / "drifted.json"
    _write_json(drifted, evidence)

    with pytest.raises(pvt_corner.PVTCornerError, match="projection differs"):
        pvt_corner.validate_existing_reference(drifted, reference_path)


def test_builder_rejects_a_symlinked_physical_root(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    link = tmp_path / "physical-link"
    link.symlink_to(values["physical"].name, target_is_directory=True)

    with pytest.raises(pvt_corner.PVTCornerError, match="symlinks"):
        pvt_corner.build_evidence(
            link,
            values["post"],
            values["pdk"],
            values["opensta"],
            values["tool_manifest"],
            values["contract"],
            tmp_path / "out",
            source_revision=REVISION,
        )


def test_source_chain_rejects_post_physical_manifest_binding_drift(
    tmp_path: Path,
) -> None:
    values = _fixture(tmp_path)
    post_path = values["post"] / "post_physical_equivalence_evidence.json"
    post = json.loads(post_path.read_text(encoding="utf-8"))
    post["backends"]["shared_dag"]["attempts"][0]["physical_run_manifest"][
        "sha256"
    ] = "0" * 64
    _write_json(post_path, post)

    with pytest.raises(pvt_corner.PVTCornerError, match="manifest binding differs"):
        pvt_corner.validate_source_chain(
            values["physical"],
            values["post"],
            values["pdk"],
            values["opensta"],
            values["tool_manifest"],
            values["contract"],
            source_revision=REVISION,
        )


def test_raw_report_replay_detects_tampering(tmp_path: Path) -> None:
    executable, _ = _make_opensta(tmp_path, "a" * 40)
    workdir = tmp_path / "run"
    record = _opensta.run_opensta(
        executable=executable,
        workdir=workdir,
        script='puts "HEPHAESTUS_PVT_CORNER=slow"\n',
        label="slow",
        replay=1,
        timeout_seconds=30,
    )
    (workdir / "stdout.txt").write_text("tampered\n", encoding="utf-8")

    with pytest.raises(pvt_corner.PVTCornerError, match="digest changed"):
        _opensta.replay_run(workdir, record, expected_label="slow")


def test_stable_projection_excludes_run_ids(tmp_path: Path) -> None:
    values = _fixture(tmp_path)
    first_out = tmp_path / "first"
    second_out = tmp_path / "second"
    first = pvt_corner.build_evidence(
        values["physical"],
        values["post"],
        values["pdk"],
        values["opensta"],
        values["tool_manifest"],
        values["contract"],
        first_out,
        source_revision=REVISION,
    )
    second = pvt_corner.build_evidence(
        values["physical"],
        values["post"],
        values["pdk"],
        values["opensta"],
        values["tool_manifest"],
        values["contract"],
        second_out,
        source_revision=REVISION,
    )
    first["execution"]["upstream_physical_workflow_run_id"] = "111"
    second["execution"]["upstream_physical_workflow_run_id"] = "222"

    assert _reference.stable_projection(first) == _reference.stable_projection(second)
