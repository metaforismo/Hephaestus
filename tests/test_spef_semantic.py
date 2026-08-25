from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

import hephaestus.spef_semantic as spef_semantic
from hephaestus.spef_semantic import _builder, _common, _reference

_REVISION = "1" * 40
_BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")


def _spef(
    *,
    design: str = "tile",
    date: str = "today",
    mapped_net: str = "*1",
    mapped_cell: str = "*2",
    name_map: tuple[str, str] = ("*1 net_a", "*2 U1"),
    reverse: bool = False,
    equivalent_units: bool = False,
    design_flow: tuple[str, ...] = ("NAME_SCOPE LOCAL", "PIN_CAP NONE"),
) -> str:
    ports = ["in I", "out O"]
    connections = [
        "*P in I",
        f"*I {mapped_cell}:A I *D cell",
        f"*I {mapped_cell}:Y O *D cell",
        "*P out O",
    ]
    capacitances = [
        "1 in 0.1",
        f"2 {mapped_cell}:A 0.05",
        f"3 {mapped_cell}:Y 0.05",
        "4 out 0.1",
    ]
    resistances = [
        f"1 in {mapped_cell}:A 1",
        f"2 {mapped_cell}:A {mapped_cell}:Y 2",
        f"3 {mapped_cell}:Y out 1",
    ]
    if reverse:
        ports.reverse()
        connections.reverse()
        capacitances.reverse()
        resistances.reverse()
    if equivalent_units:
        time_unit = "*T_UNIT 1000 PS"
        cap_unit = "*C_UNIT 1000 FF"
        resistance_unit = "*R_UNIT 0.001 KOHM"
    else:
        time_unit = "*T_UNIT 1 NS"
        cap_unit = "*C_UNIT 1 PF"
        resistance_unit = "*R_UNIT 1 OHM"
    return "\n".join(
        [
            '*SPEF "ieee 1481-1999"',
            f'*DESIGN "{design}"',
            f'*DATE "{date}"',
            '*VENDOR "fixture"',
            '*PROGRAM "fixture"',
            '*VERSION "1"',
            "*DESIGN_FLOW " + " ".join(f'"{value}"' for value in design_flow),
            "*DIVIDER /",
            "*DELIMITER :",
            "*BUS_DELIMITER []",
            time_unit,
            cap_unit,
            resistance_unit,
            "*L_UNIT 1 HENRY",
            "",
            "*NAME_MAP",
            *name_map,
            "",
            "*PORTS",
            *ports,
            "",
            f"*D_NET {mapped_net} 0.3",
            "*CONN",
            *connections,
            "*CAP",
            *capacitances,
            "*RES",
            *resistances,
            "*END",
            "",
        ]
    )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _claims(*, post_physical: bool) -> dict[str, bool]:
    if post_physical:
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


def _make_artifacts(tmp_path: Path) -> tuple[Path, Path]:
    physical = tmp_path / "physical"
    post = tmp_path / "post"
    physical_backends: dict[str, object] = {}
    post_backends: dict[str, object] = {}
    for backend in _BACKENDS:
        runs: list[dict[str, object]] = []
        normalized: list[str] = []
        for attempt in (1, 2):
            attempt_root = (
                physical
                / "downloaded-runs"
                / f"openroad-physical-run-{backend}-{attempt}"
            )
            spef = attempt_root / "results" / "6_final.spef"
            spef.parent.mkdir(parents=True, exist_ok=True)
            spef.write_text(
                _spef(design=f"tile_{backend}", date=f"attempt {attempt}"),
                encoding="utf-8",
            )
            spef_meta = {
                "path": "results/6_final.spef",
                "sha256": _common._sha256(spef),
                "size_bytes": spef.stat().st_size,
            }
            normalized_digest = _builder._date_normalized_sha256(spef)
            normalized.append(normalized_digest)
            manifest = {
                "schema": "hephaestus.openroad-physical-run.v1",
                "identity": {"backend": backend, "attempt": attempt},
                "artifacts": {"final_spef": spef_meta},
                "normalized": {
                    "spef_date_normalized_sha256": normalized_digest,
                },
                "claims": {
                    "registered_source_binding_verified": True,
                    "pinned_orfs_image_used": True,
                    "placement_performed": True,
                    "routing_performed": True,
                    "spef_generated": True,
                    "post_physical_equivalence_verified": False,
                    "drc_clean": False,
                    "lvs_clean": False,
                    "power_estimated_with_activity": False,
                    "post_layout_pex_verified": False,
                    "foundry_signoff_complete": False,
                    "silicon_verified": False,
                },
            }
            original_manifest = attempt_root / "openroad_run.json"
            _write_json(original_manifest, manifest)
            manifest_digest = _common._sha256(original_manifest)
            bound_relative = f"run_manifests/{backend}-attempt-{attempt:02d}.json"
            bound_manifest = physical / "evidence" / bound_relative
            bound_manifest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original_manifest, bound_manifest)
            runs.append(
                {
                    "attempt": attempt,
                    "manifest": bound_relative,
                    "manifest_sha256": manifest_digest,
                    "artifacts": {"final_spef": spef_meta},
                }
            )
        assert len(set(normalized)) == 1
        physical_backends[backend] = {
            "repeatability": {"passed": True},
            "runs": runs,
        }
        post_backends[backend] = {
            "passed": True,
            "both_physical_attempts_bound": True,
            "attempts": [
                {
                    "attempt": run["attempt"],
                    "physical_run_manifest": {
                        "sha256": run["manifest_sha256"],
                    },
                }
                for run in runs
            ],
        }

    physical_manifest = physical / "evidence" / "openroad_physical_evidence.json"
    _write_json(
        physical_manifest,
        {
            "schema": "hephaestus.openroad-physical-evidence.v1",
            "evidence_level": "matched_registered_orfs_rtl_to_gds_repeatability",
            "claims": _claims(post_physical=False),
            "backends": physical_backends,
        },
    )
    physical_digest = _common._sha256(physical_manifest)
    post_source = post / "source" / "openroad_physical_evidence.json"
    post_source.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(physical_manifest, post_source)
    _write_json(
        post / "post_physical_equivalence_evidence.json",
        {
            "schema": "hephaestus.post-physical-equivalence-evidence.v1",
            "evidence_level": "exact_registered_source_to_routed_sequential_equivalence",
            "execution": {"source_revision": _REVISION},
            "source": {"physical_evidence_sha256": physical_digest},
            "claims": _claims(post_physical=True),
            "regression": {"passed": True},
            "backends": post_backends,
        },
    )
    return physical, post


def _make_reference(
    physical: Path,
    post: Path,
    tmp_path: Path,
) -> Path:
    bootstrap = tmp_path / "bootstrap"
    evidence = _builder._collect_evidence(
        physical,
        post,
        bootstrap,
        source_revision=_REVISION,
    )
    reference = tmp_path / "reference.json"
    _write_json(reference, _reference.make_reference(evidence))
    shutil.rmtree(bootstrap)
    return reference


def test_canonicalization_ignores_order_name_map_ids_date_and_equivalent_units() -> None:
    first = spef_semantic.parse_spef_text(_spef(date="first"))
    second = spef_semantic.parse_spef_text(
        _spef(
            date="second",
            mapped_net="*7",
            mapped_cell="*9",
            name_map=("*9 U1", "*7 net_a"),
            reverse=True,
            equivalent_units=True,
        )
    )

    assert first["canonical_sha256"] == second["canonical_sha256"]
    assert first["unit_contract"] == second["unit_contract"]
    assert first["producer"]["date"] != second["producer"]["date"]


def test_semantic_design_flow_changes_the_canonical_digest() -> None:
    baseline = spef_semantic.parse_spef_text(_spef())
    changed = spef_semantic.parse_spef_text(
        _spef(design_flow=("NAME_SCOPE LOCAL", "PIN_CAP INPUT_OUTPUT"))
    )

    assert changed["canonical_sha256"] != baseline["canonical_sha256"]
    assert changed["design_flow"] == [
        "NAME_SCOPE LOCAL",
        "PIN_CAP INPUT_OUTPUT",
    ]


def test_parser_rejects_declared_capacitance_drift() -> None:
    text = _spef().replace("*D_NET *1 0.3", "*D_NET *1 1.3")

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="declared capacitance differs",
    ):
        spef_semantic.parse_spef_text(text)


def test_parser_rejects_an_unsupported_unit() -> None:
    text = _spef().replace("*C_UNIT 1 PF", "*C_UNIT 1 FURLONG")

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match=r"unsupported \*C_UNIT unit",
    ):
        spef_semantic.parse_spef_text(text)


def test_parser_rejects_an_undefined_name_map_reference() -> None:
    text = _spef().replace("*D_NET *1 0.3", "*D_NET *99 0.3")

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="undefined name-map entry",
    ):
        spef_semantic.parse_spef_text(text)


def test_resistance_change_changes_the_canonical_digest() -> None:
    original = spef_semantic.parse_spef_text(_spef())
    mutated = spef_semantic.parse_spef_text(
        _spef().replace("2 *2:A *2:Y 2", "2 *2:A *2:Y 3")
    )

    assert original["canonical_sha256"] != mutated["canonical_sha256"]
    assert original["metrics"]["total_resistance_ohm"] == "4"
    assert mutated["metrics"]["total_resistance_ohm"] == "5"


def test_end_to_end_builder_binds_six_spefs_and_nine_controls(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    reference = _make_reference(physical, post, tmp_path)
    output = tmp_path / "qualified"

    evidence = spef_semantic.build_evidence(
        physical,
        post,
        reference,
        output,
        source_revision=_REVISION,
    )

    assert evidence["regression"]["passed"] is True
    assert evidence["claims"]["all_six_spef_files_parsed"] is True
    assert evidence["claims"]["post_layout_pex_verified"] is False
    assert sum(len(value["attempts"]) for value in evidence["backends"].values()) == 6
    assert (
        sum(len(value["negative_controls"]) for value in evidence["backends"].values())
        == 9
    )
    assert (output / "spef_semantic_evidence.json").is_file()
    assert (output / "SUMMARY.md").is_file()


def test_builder_rejects_an_existing_output_without_deleting_it(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    reference = _make_reference(physical, post, tmp_path)
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep\n", encoding="utf-8")

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="already exists",
    ):
        spef_semantic.build_evidence(physical, post, reference, output)

    assert sentinel.read_text(encoding="utf-8") == "keep\n"


def test_builder_rejects_a_symlinked_public_input(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    reference = _make_reference(physical, post, tmp_path)
    physical_link = tmp_path / "physical-link"
    physical_link.symlink_to(physical.name, target_is_directory=True)

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="must not contain symlinks",
    ):
        spef_semantic.build_evidence(
            physical_link,
            post,
            reference,
            tmp_path / "output",
        )


def test_builder_rejects_another_post_physical_source_revision(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    reference = _make_reference(physical, post, tmp_path)

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="another source revision",
    ):
        spef_semantic.build_evidence(
            physical,
            post,
            reference,
            tmp_path / "output",
            source_revision="2" * 40,
        )


def test_builder_records_the_bound_post_physical_revision_when_omitted(
    tmp_path: Path,
) -> None:
    physical, post = _make_artifacts(tmp_path)
    reference = _make_reference(physical, post, tmp_path)

    evidence = spef_semantic.build_evidence(
        physical,
        post,
        reference,
        tmp_path / "output",
    )

    assert evidence["execution"]["source_revision"] == _REVISION


def test_builder_rejects_post_physical_attempt_binding_drift(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    post_manifest = post / "post_physical_equivalence_evidence.json"
    value = json.loads(post_manifest.read_text(encoding="utf-8"))
    value["backends"]["shared_dag"]["attempts"][0]["physical_run_manifest"][
        "sha256"
    ] = "0" * 64
    _write_json(post_manifest, value)

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="post-physical manifest binding differs",
    ):
        _builder._collect_evidence(
            physical,
            post,
            tmp_path / "output",
            source_revision=_REVISION,
        )


def test_builder_rejects_a_symlinked_manifest_spef(tmp_path: Path) -> None:
    physical, post = _make_artifacts(tmp_path)
    spef = next(physical.rglob("6_final.spef"))
    real = spef.with_name("real.spef")
    spef.rename(real)
    spef.symlink_to(real.name)

    with pytest.raises(
        spef_semantic.SPEFSemanticError,
        match="must not contain symlinks",
    ):
        _builder._collect_evidence(
            physical,
            post,
            tmp_path / "output",
            source_revision=_REVISION,
        )
