from __future__ import annotations

import json
from pathlib import Path

import pytest

from hephaestus.openroad_physical import (
    OpenROADPhysicalError,
    bind_physical_evidence,
    emit_config,
    emit_sdc,
    gds_timestamp_normalized_sha256,
    parse_def_metrics,
    prepare_physical_evidence,
    spef_date_normalized_sha256,
    stable_metadata_metrics,
)
from hephaestus.report import sha256_file

BACKENDS = ("shared_dag", "naive_shift_add", "constant_multipliers")


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _record(record_type: int, payload: bytes, data_type: int = 0) -> bytes:
    length = len(payload) + 4
    return length.to_bytes(2, "big") + bytes([record_type, data_type]) + payload


def _fake_gds(timestamp_byte: int, geometry_byte: int = 7) -> bytes:
    return b"".join(
        [
            _record(0x00, b"\x00\x07", 2),
            _record(0x01, bytes([timestamp_byte]) * 24, 2),
            _record(0x05, bytes([timestamp_byte + 1]) * 24, 2),
            _record(0x08, bytes([geometry_byte, 0]), 0),
            _record(0x04, b"", 0),
        ]
    )


def _contract() -> dict[str, object]:
    return {
        "schema": "hephaestus.openroad-physical-contract.v1",
        "contract_id": "ihp-sg13g2-openroad-registered-v1",
        "platform": "ihp-sg13g2",
        "backends": list(BACKENDS),
        "attempts_per_backend": 2,
        "clock": {
            "name": "core_clock",
            "port": "clk",
            "period_ns": 4.0,
            "input_delay_ns": 0.2,
            "output_delay_ns": 0.2,
            "uncertainty_ns": 0.1,
        },
        "floorplan": {
            "die_area_um": [0.0, 0.0, 240.0, 240.0],
            "core_area_um": [20.0, 20.0, 220.0, 220.0],
            "place_density": 0.5,
        },
        "io": {
            "input_driver_cell": "sg13g2_buf_4",
            "output_load_pf": 0.01,
        },
        "routing": {
            "minimum_layer": "Metal2",
            "maximum_layer": "Metal5",
        },
        "openroad_threads": 1,
    }


def _registered_claims() -> dict[str, bool]:
    return {
        "source_matched_integer_contract_verified": True,
        "source_exhaustive_combinational_equivalence_verified": True,
        "source_formal_negative_control_counterexample_found": True,
        "registered_streaming_interface_generated": True,
        "registered_backends_match_oracle_on_executed_schedule": True,
        "one_cycle_latency_verified_on_executed_schedule": True,
        "initiation_interval_one_verified_on_executed_schedule": True,
        "reset_flush_verified_on_executed_schedule": True,
        "simulation_negative_control_detected": True,
        "sequential_formal_equivalence_verified": False,
        "post_synthesis_ppa_measured": False,
        "placement_performed": False,
        "routing_performed": False,
        "power_estimated": False,
        "post_layout_pex_verified": False,
        "silicon_verified": False,
    }


def _source_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path, Path]:
    bundle = tmp_path / "registered"
    bundle.mkdir()
    contract = {
        "domain": "quantized_integer_core_before_row_scaling",
        "input_count": 2,
        "output_count": 2,
        "input_width": 4,
        "accumulator_width": 6,
        "input_bits": 8,
        "output_bits": 12,
        "latency_cycles": 1,
        "valid_latency_cycles": 1,
        "initiation_interval_cycles": 1,
        "clock_edge": "rising",
        "reset_style": "synchronous_active_high",
        "reset_clears_pipeline": True,
        "input_registered": True,
        "output_registered": True,
        "combinational_core_preserved": True,
        "runtime_coefficient_reads_per_matvec": 0,
    }
    backend_specs: dict[str, object] = {}
    core_hashes: dict[str, str] = {}
    wrapper_hashes: dict[str, str] = {}
    for backend in BACKENDS:
        core = bundle / f"{backend}_core.sv"
        wrapper = bundle / f"{backend}_registered.sv"
        core.write_text(f"module core_{backend}; endmodule\n", encoding="utf-8")
        wrapper.write_text(f"module wrapper_{backend}; endmodule\n", encoding="utf-8")
        core_hashes[backend] = sha256_file(core)
        wrapper_hashes[backend] = sha256_file(wrapper)
        backend_specs[backend] = {
            "core_module": f"core_{backend}",
            "wrapper_module": f"wrapper_{backend}",
            "core_rtl": core.name,
            "wrapper_rtl": wrapper.name,
            "core_sha256": core_hashes[backend],
            "wrapper_sha256": wrapper_hashes[backend],
            "runtime_coefficient_reads_per_matvec": 0,
        }
    manifest_path = bundle / "registered_manifest.json"
    _write_json(
        manifest_path,
        {
            "schema": "hephaestus.registered-matched-tiles.v1",
            "contract": contract,
            "backends": backend_specs,
            "claims": _registered_claims(),
        },
    )

    registered_reference = tmp_path / "registered_reference.json"
    _write_json(
        registered_reference,
        {
            "schema": "hephaestus.registered-matched-tiles-reference.v1",
            "reference_id": "registered-matched-tiles-tiny-v1",
            "contract": contract,
            "source_artifacts": {"core_sha256": core_hashes},
            "generated_artifacts": {"wrapper_sha256": wrapper_hashes},
        },
    )
    physical_contract = tmp_path / "physical_contract.json"
    _write_json(physical_contract, _contract())
    probe_reference = tmp_path / "probe_reference.json"
    _write_json(
        probe_reference,
        {
            "schema": "hephaestus.openroad-registered-smoke-reference.v1",
            "toolchain": {
                "orfs_image_repo_digest": "openroad/orfs@sha256:" + "1" * 64,
                "orfs_image_id": "sha256:" + "2" * 64,
                "tool_versions": {
                    "openroad": "OpenROAD test",
                    "yosys": "Yosys test",
                    "klayout": "KLayout test",
                },
                "num_cores": 1,
                "transactional_kepler_lec_enabled": False,
            },
            "physical_contract": {
                "platform": "ihp-sg13g2",
                "backend": "shared_dag",
                "clock_name": "core_clock",
                "clock_port": "clk",
                "clock_period_ns": 4.0,
                "input_delay_ns": 0.2,
                "output_delay_ns": 0.2,
                "clock_uncertainty_ns": 0.1,
                "input_driver_cell": "sg13g2_buf_4",
                "output_load_pf": 0.01,
                "die_area_um": [0.0, 0.0, 240.0, 240.0],
                "core_area_um": [20.0, 20.0, 220.0, 220.0],
                "place_density": 0.5,
                "min_routing_layer": "Metal2",
                "max_routing_layer": "Metal5",
            },
            "source": {
                "registered_manifest_sha256": sha256_file(manifest_path),
                "core_sha256": core_hashes["shared_dag"],
                "wrapper_sha256": wrapper_hashes["shared_dag"],
            },
            "case": {
                "backend": "shared_dag",
                "registered_latency_cycles": 1,
                "initiation_interval_cycles": 1,
                "runtime_coefficient_reads_per_matvec": 0,
            },
            "claims": {
                "registered_source_binding_verified": True,
                "single_backend_orfs_flow_completed": True,
                "placement_performed": True,
                "routing_performed": True,
                "gds_generated": True,
                "matched_three_backend_physical_comparison_performed": False,
                "post_physical_equivalence_verified": False,
                "drc_clean": False,
                "lvs_clean": False,
                "power_estimated_with_activity": False,
                "post_layout_pex_verified": False,
                "foundry_signoff_complete": False,
                "silicon_verified": False,
            },
        },
    )
    helpers = tmp_path / "helpers"
    helpers.mkdir()
    (helpers / "synth_compat.tcl").write_text(
        "source $::env(SCRIPTS_DIR)/synth.tcl\n",
        encoding="utf-8",
    )
    (helpers / "sanitize_yosys_netlist.py").write_text(
        "print('sanitize')\n",
        encoding="utf-8",
    )
    return bundle, registered_reference, probe_reference, physical_contract, helpers


def test_common_config_and_sdc_are_explicit() -> None:
    contract = _contract()
    backend = {
        "wrapper_module": "wrapper_shared",
        "core_rtl": "shared_core.sv",
        "wrapper_rtl": "shared_wrapper.sv",
    }

    config = emit_config(contract, backend_name="shared_dag", backend=backend)
    sdc = emit_sdc(contract)

    assert "export PLATFORM = ihp-sg13g2" in config
    assert "export DESIGN_NAME = wrapper_shared" in config
    assert "export DIE_AREA = 0 0 240 240" in config
    assert "export CORE_AREA = 20 20 220 220" in config
    assert "export PLACE_DENSITY = 0.5" in config
    assert "export MIN_ROUTING_LAYER = Metal2" in config
    assert "export MAX_ROUTING_LAYER = Metal5" in config
    assert "export SYNTH_SCRIPT = $(dir $(DESIGN_CONFIG))/synth_compat.tcl" in config
    assert "export LEC_CHECK = 0" in config
    verilog_line = next(
        line for line in config.splitlines() if line.startswith("export VERILOG_FILES")
    )
    assert verilog_line[-1] == chr(92)
    assert verilog_line[-2] != chr(92)
    assert "create_clock -name core_clock -period 4" in sdc
    assert "set data_inputs [all_inputs -no_clocks]" in sdc
    assert "remove_from_collection" not in sdc
    assert "set_clock_uncertainty 0.1" in sdc
    assert "set_driving_cell -lib_cell sg13g2_buf_4" in sdc
    assert "set_load 0.01" in sdc


def test_gds_digest_ignores_only_library_and_structure_timestamps(tmp_path: Path) -> None:
    first = tmp_path / "first.gds"
    second = tmp_path / "second.gds"
    changed = tmp_path / "changed.gds"
    first.write_bytes(_fake_gds(1, geometry_byte=7))
    second.write_bytes(_fake_gds(9, geometry_byte=7))
    changed.write_bytes(_fake_gds(9, geometry_byte=8))

    assert gds_timestamp_normalized_sha256(first) == gds_timestamp_normalized_sha256(second)
    assert gds_timestamp_normalized_sha256(first) != gds_timestamp_normalized_sha256(changed)


def test_spef_digest_ignores_only_the_date_record(tmp_path: Path) -> None:
    first = tmp_path / "first.spef"
    second = tmp_path / "second.spef"
    changed = tmp_path / "changed.spef"
    first.write_text(
        '*SPEF "IEEE 1481-1998"\n*DATE "one"\n*R_UNIT 1 OHM\n',
        encoding="utf-8",
    )
    second.write_text(
        '*SPEF "IEEE 1481-1998"\n*DATE "two"\n*R_UNIT 1 OHM\n',
        encoding="utf-8",
    )
    changed.write_text(
        '*SPEF "IEEE 1481-1998"\n*DATE "two"\n*R_UNIT 2 OHM\n',
        encoding="utf-8",
    )

    assert spef_date_normalized_sha256(first) == spef_date_normalized_sha256(second)
    assert spef_date_normalized_sha256(first) != spef_date_normalized_sha256(changed)


def test_def_parser_extracts_common_geometry_and_counts(tmp_path: Path) -> None:
    path = tmp_path / "final.def"
    path.write_text(
        """VERSION 5.8 ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 240000 240000 ) ;
ROW ROW_0 CoreSite 0 0 N DO 10 BY 1 STEP 480 0 ;
TRACKS X 0 DO 10 STEP 420 LAYER Metal1 ;
COMPONENTS 12 ;
END COMPONENTS
NETS 9 ;
END NETS
SPECIALNETS 2 ;
END SPECIALNETS
PINS 4 ;
END PINS
VIAS 3 ;
END VIAS
END DESIGN
""",
        encoding="utf-8",
    )

    metrics = parse_def_metrics(path)

    assert metrics["die_area_um"] == [0.0, 0.0, 240.0, 240.0]
    assert metrics["die_width_um"] == 240.0
    assert metrics["component_count"] == 12
    assert metrics["net_count"] == 9
    assert metrics["special_net_count"] == 2
    assert metrics["pin_count"] == 4
    assert metrics["via_definition_count"] == 3
    assert metrics["row_count"] == 1
    assert metrics["track_statement_count"] == 1


def test_metadata_selector_excludes_runtime_noise() -> None:
    selected = stable_metadata_metrics(
        {
            "finish__design__instance__area__stdcell": 123.5,
            "finish__timing__setup__ws": -0.02,
            "finish__route__wirelength": 88,
            "run__runtime__seconds": 91.0,
            "globalroute__global_route__fastroute__route_l_s": 0.0006,
            "tool__version": "x",
            "design": "name",
        }
    )

    assert selected == {
        "finish__design__instance__area__stdcell": 123.5,
        "finish__route__wirelength": 88,
        "finish__timing__setup__ws": -0.02,
    }


def test_prepare_binds_registered_sources_and_probe(tmp_path: Path) -> None:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )

    manifest = prepare_physical_evidence(
        bundle,
        registered_reference,
        probe_reference,
        physical_contract,
        tmp_path / "prepared",
        helper_dir=helpers,
    )

    assert manifest["schema"] == "hephaestus.openroad-physical-prepared.v1"
    assert set(manifest["backends"]) == set(BACKENDS)
    assert manifest["claims"]["registered_source_binding_verified"]
    assert manifest["claims"]["orfs_image_digest_pinned"]
    assert manifest["claims"]["placement_performed"] is False
    assert (tmp_path / "prepared/prepared.json").is_file()
    for backend in BACKENDS:
        design = tmp_path / "prepared" / manifest["backends"][backend]["design_dir"]
        assert (design / "config.mk").is_file()
        assert (design / "constraint.sdc").is_file()
        assert (design / "rules.json").is_file()
        assert (design / "synth_compat.tcl").is_file()
        assert (design / "sanitize_yosys_netlist.py").is_file()
        assert manifest["backends"][backend]["synth_compat_sha256"]
        assert manifest["backends"][backend]["sanitizer_sha256"]


def test_prepare_rejects_a_probe_for_different_shared_rtl(tmp_path: Path) -> None:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )
    probe = json.loads(probe_reference.read_text(encoding="utf-8"))
    probe["source"]["core_sha256"] = "f" * 64
    _write_json(probe_reference, probe)

    with pytest.raises(OpenROADPhysicalError, match="qualifying smoke run"):
        prepare_physical_evidence(
            bundle,
            registered_reference,
            probe_reference,
            physical_contract,
            tmp_path / "prepared",
            helper_dir=helpers,
        )


def test_prepare_rejects_probe_io_contract_drift(tmp_path: Path) -> None:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )
    probe = json.loads(probe_reference.read_text(encoding="utf-8"))
    probe["physical_contract"]["input_delay_ns"] = 0.3
    _write_json(probe_reference, probe)

    with pytest.raises(OpenROADPhysicalError, match="input_delay_ns differs"):
        prepare_physical_evidence(
            bundle,
            registered_reference,
            probe_reference,
            physical_contract,
            tmp_path / "prepared",
            helper_dir=helpers,
        )


def test_prepare_rejects_missing_physical_helpers(tmp_path: Path) -> None:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )
    (helpers / "synth_compat.tcl").unlink()

    with pytest.raises(OpenROADPhysicalError, match="physical helper"):
        prepare_physical_evidence(
            bundle,
            registered_reference,
            probe_reference,
            physical_contract,
            tmp_path / "prepared",
            helper_dir=helpers,
        )


def test_prepare_rejects_an_unverified_registered_bundle(tmp_path: Path) -> None:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )
    manifest_path = bundle / "registered_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["claims"]["reset_flush_verified_on_executed_schedule"] = False
    _write_json(manifest_path, manifest)

    with pytest.raises(OpenROADPhysicalError, match="not fully qualified"):
        prepare_physical_evidence(
            bundle,
            registered_reference,
            probe_reference,
            physical_contract,
            tmp_path / "prepared",
            helper_dir=helpers,
        )


def _artifact(path: Path, root: Path) -> dict[str, object]:
    return {
        "path": path.relative_to(root).as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _write_run(
    prepared_path: Path,
    run_root: Path,
    *,
    backend: str,
    attempt: int,
    geometry_byte: int = 7,
    metric_area: float = 100.0,
    spef_resistance: int = 1,
) -> Path:
    prepared = json.loads(prepared_path.read_text(encoding="utf-8"))
    run_root.mkdir(parents=True, exist_ok=True)
    variant = f"attempt-{attempt:02d}"
    output = run_root / "results" / "ihp-sg13g2" / f"design_{backend}" / variant
    reports = run_root / "reports" / "ihp-sg13g2" / f"design_{backend}" / variant
    provenance = run_root / "provenance"
    output.mkdir(parents=True)
    reports.mkdir(parents=True)
    provenance.mkdir()

    gds = output / "6_final.gds"
    final_def = output / "6_final.def"
    final_verilog = output / "6_final.v"
    final_odb = output / "6_final.odb"
    final_spef = output / "6_final.spef"
    metadata = reports / "metadata.json"
    gds.write_bytes(_fake_gds(attempt, geometry_byte=geometry_byte))
    final_def.write_text(
        """VERSION 5.8 ;
UNITS DISTANCE MICRONS 1000 ;
DIEAREA ( 0 0 ) ( 240000 240000 ) ;
ROW ROW_0 CoreSite 0 0 N DO 10 BY 1 STEP 480 0 ;
TRACKS X 0 DO 10 STEP 420 LAYER Metal1 ;
COMPONENTS 12 ;
END COMPONENTS
NETS 9 ;
END NETS
SPECIALNETS 2 ;
END SPECIALNETS
PINS 4 ;
END PINS
VIAS 3 ;
END VIAS
END DESIGN
""",
        encoding="utf-8",
    )
    final_verilog.write_text("module final; endmodule\n", encoding="utf-8")
    final_odb.write_bytes(f"odb-{backend}-{attempt}".encode())
    final_spef.write_text(
        (f'*SPEF "IEEE 1481-1998"\n*DATE "attempt-{attempt}"\n*R_UNIT {spef_resistance} OHM\n'),
        encoding="utf-8",
    )
    _write_json(
        metadata,
        {
            "finish__design__instance__area__stdcell": metric_area,
            "finish__timing__setup__ws": 0.25,
            "finish__route__wirelength": 200,
            "run__runtime__seconds": 10 + attempt,
        },
    )
    image_inspect = provenance / "orfs-image-inspect.json"
    tool_versions = provenance / "tool-versions.txt"
    image_inspect.write_text("[]\n", encoding="utf-8")
    tool_versions.write_text("OpenROAD test\n", encoding="utf-8")
    stdout = run_root / "orfs.stdout.txt"
    stderr = run_root / "orfs.stderr.txt"
    returncode = run_root / "orfs.returncode.txt"
    stdout.write_text("ok\n", encoding="utf-8")
    stderr.write_text("", encoding="utf-8")
    returncode.write_text("0\n", encoding="utf-8")

    backend_spec = prepared["backends"][backend]
    artifacts = {
        "final_gds": _artifact(gds, run_root),
        "final_def": _artifact(final_def, run_root),
        "final_verilog": _artifact(final_verilog, run_root),
        "final_odb": _artifact(final_odb, run_root),
        "final_spef": _artifact(final_spef, run_root),
        "metadata": _artifact(metadata, run_root),
    }
    manifest = {
        "schema": "hephaestus.openroad-physical-run.v1",
        "evidence_level": "single_registered_backend_orfs_rtl_to_gds",
        "identity": {
            "backend": backend,
            "attempt": attempt,
            "variant": variant,
        },
        "source": {
            "prepared_manifest_sha256": sha256_file(prepared_path),
            "registered_manifest_sha256": prepared["source"]["registered_manifest_sha256"],
            "core_sha256": backend_spec["core_sha256"],
            "wrapper_sha256": backend_spec["wrapper_sha256"],
            "config_sha256": backend_spec["config_sha256"],
            "sdc_sha256": backend_spec["sdc_sha256"],
            "rules_sha256": backend_spec["rules_sha256"],
            "synth_compat_sha256": backend_spec["synth_compat_sha256"],
            "sanitizer_sha256": backend_spec["sanitizer_sha256"],
            "contract_sha256": prepared["contract"]["sha256"],
        },
        "toolchain": {
            "orfs_image_repo_digest": prepared["toolchain"]["orfs_image_repo_digest"],
            "image_inspect": _artifact(image_inspect, run_root),
            "tool_versions": _artifact(tool_versions, run_root),
        },
        "artifacts": artifacts,
        "normalized": {
            "gds_timestamp_normalized_sha256": gds_timestamp_normalized_sha256(gds),
            "spef_date_normalized_sha256": spef_date_normalized_sha256(final_spef),
            "def_metrics": parse_def_metrics(final_def),
            "stable_metadata_metrics": stable_metadata_metrics(
                json.loads(metadata.read_text(encoding="utf-8"))
            ),
        },
        "execution": {
            "returncode": 0,
            "stdout": _artifact(stdout, run_root),
            "stderr": _artifact(stderr, run_root),
            "returncode_file": _artifact(returncode, run_root),
        },
        "claims": {
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
        },
    }
    manifest_path = run_root / "openroad_run.json"
    _write_json(manifest_path, manifest)
    return manifest_path


def _prepared_fixture(tmp_path: Path) -> Path:
    bundle, registered_reference, probe_reference, physical_contract, helpers = _source_fixture(
        tmp_path
    )
    prepare_physical_evidence(
        bundle,
        registered_reference,
        probe_reference,
        physical_contract,
        tmp_path / "prepared",
        helper_dir=helpers,
    )
    return tmp_path / "prepared/prepared.json"


def test_binder_accepts_six_repeatable_runs(tmp_path: Path) -> None:
    prepared = _prepared_fixture(tmp_path)
    runs = tmp_path / "runs"
    for backend_index, backend in enumerate(BACKENDS):
        for attempt in (1, 2):
            _write_run(
                prepared,
                runs / backend / f"attempt-{attempt:02d}",
                backend=backend,
                attempt=attempt,
                geometry_byte=7 + backend_index,
                metric_area=100.0 + backend_index,
            )

    evidence = bind_physical_evidence(prepared, runs, tmp_path / "evidence")

    assert evidence["schema"] == "hephaestus.openroad-physical-evidence.v1"
    assert set(evidence["backends"]) == set(BACKENDS)
    assert evidence["claims"]["all_three_backends_routed"]
    assert evidence["claims"]["physical_repeatability_verified"]
    assert all(
        backend["repeatability"]["spef_date_normalized_identical"]
        for backend in evidence["backends"].values()
    )
    assert evidence["claims"]["post_physical_equivalence_verified"] is False
    assert evidence["claims"]["comparative_ppa_claim_enabled"] is False
    assert (tmp_path / "evidence/SUMMARY.md").is_file()


def test_binder_rejects_a_missing_attempt(tmp_path: Path) -> None:
    prepared = _prepared_fixture(tmp_path)
    runs = tmp_path / "runs"
    for backend in BACKENDS:
        for attempt in (1, 2):
            if backend == "shared_dag" and attempt == 2:
                continue
            _write_run(
                prepared,
                runs / backend / f"attempt-{attempt:02d}",
                backend=backend,
                attempt=attempt,
            )

    with pytest.raises(OpenROADPhysicalError, match="expected 6 physical run manifests"):
        bind_physical_evidence(prepared, runs, tmp_path / "evidence")


def test_binder_rejects_gds_geometry_drift(tmp_path: Path) -> None:
    prepared = _prepared_fixture(tmp_path)
    runs = tmp_path / "runs"
    for backend in BACKENDS:
        for attempt in (1, 2):
            geometry = 9 if backend == "shared_dag" and attempt == 2 else 7
            _write_run(
                prepared,
                runs / backend / f"attempt-{attempt:02d}",
                backend=backend,
                attempt=attempt,
                geometry_byte=geometry,
            )

    with pytest.raises(OpenROADPhysicalError, match="repeatability failed"):
        bind_physical_evidence(prepared, runs, tmp_path / "evidence")


def test_binder_rejects_spef_parasitic_drift(tmp_path: Path) -> None:
    prepared = _prepared_fixture(tmp_path)
    runs = tmp_path / "runs"
    for backend in BACKENDS:
        for attempt in (1, 2):
            _write_run(
                prepared,
                runs / backend / f"attempt-{attempt:02d}",
                backend=backend,
                attempt=attempt,
                spef_resistance=(2 if backend == "shared_dag" and attempt == 2 else 1),
            )

    with pytest.raises(OpenROADPhysicalError, match="repeatability failed"):
        bind_physical_evidence(prepared, runs, tmp_path / "evidence")


def test_binder_rejects_a_tampered_source_binding(tmp_path: Path) -> None:
    prepared = _prepared_fixture(tmp_path)
    runs = tmp_path / "runs"
    first_path: Path | None = None
    for backend in BACKENDS:
        for attempt in (1, 2):
            path = _write_run(
                prepared,
                runs / backend / f"attempt-{attempt:02d}",
                backend=backend,
                attempt=attempt,
            )
            if first_path is None:
                first_path = path
    assert first_path is not None
    manifest = json.loads(first_path.read_text(encoding="utf-8"))
    manifest["source"]["wrapper_sha256"] = "0" * 64
    _write_json(first_path, manifest)

    with pytest.raises(OpenROADPhysicalError, match="source binding differs"):
        bind_physical_evidence(prepared, runs, tmp_path / "evidence")
