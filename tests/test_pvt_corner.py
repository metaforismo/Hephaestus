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


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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
        "pinned_orfs_imae_used": True,
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
    subprocess.run(["git", "config", "user.name", "fixture"], cwd=root, check=True)
    subprocess.run(
        ["git", "config", "user.email", "fixture@example.invalid"],
        cwd=root,
        check=True,
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    commit = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=root, text=True
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
  typ) slack=0.5; status=MET; tns=0.0 ;
  fast) slack=0.75; status=MET; tns=0.0 ;
  *) slack=-1.25; status=VIOLATED; tns=-2.5 ;;
esac
printf 'HEPHAESTUS_PVT_CORNER=%s\n' "$label"
printf '%s slack (%s)\n' "$slack" "$status"
printf 'tns %s\n' "$tns"
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
    post_bac²È="25™É½µ}Í¡„ÈÔØ¡ÑµÁ}Á…Ñ èA…Ñ ¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€½¹ÑÉ…Ð€ô©Í½¸¹±½…‘Ì¡Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤(€€€½¹ÑÉ…Ñl‰¥¡Á}½Á•¹}Á‘¬‰ul‰½µµ¥Ð‰t€ô€‰˜ˆ€¨€ØÐ(€€€}ÝÉ¥Ñ•}©Í½¸¡Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°½¹ÑÉ…Ð¤((€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ôˆÐÀµ¡…É…Ñ•È¥ÐM!ˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Ù…±¥‘…Ñ•}½¹ÑÉ…Ð¡Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t¤(()‘•˜Ñ•ÍÑ}Ñ¥¡Ñ•¹}Í‘}É•ÅÕ¥É•Í}•á…Ñ±å}½¹•}±½¬ ¤€´ø9½¹”è(€€€…ÍÍ•ÉÐ€ˆµÁ•É¥½€À¸ÀÔˆ¥¸ÁÙÑ}½É¹•È¹Ñ¥¡Ñ•¹}Í‘Œ (€€€€€€€€‰É•…Ñ•}±½¬€µÁ•É¥½€Ð¸Àm•Ñ}Á½ÉÑÌ±­uq¸ˆ°(€€€€€€€€À¸ÀÔ°(€€€€¤(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰•á…Ñ±ä½¹”ˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Ñ¥¡Ñ•¹}Í‘Œ (€€€€€€€€€€€€‰É•…Ñ•}±½¬€µÁ•É¥½€Ð¸Àm•Ñ}Á½ÉÑÌ…uq¸ˆ(€€€€€€€€€€€€‰É•…Ñ•}±½¬€µÁ•É¥½€Ô¸Àm•Ñ}Á½ÉÑÌ‰uq¸ˆ°(€€€€€€€€€€€€À¸ÀÔ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}Á…ÉÍ•}½Á•¹ÍÑ…}½ÕÑÁÕÑ}É•ÅÕ¥É•Í}µ…É­•É}…¹‘}½¹Í¥ÍÑ•¹Ñ}ÍÑ…ÑÕÌ ¤€´ø9½¹”è(€€€Ù…±Õ”€ôÁÙÑ}½É¹•È¹Á…ÉÍ•}½Á•¹ÍÑ…}½ÕÑÁÕÐ (€€€€€€€€‰!A!MQUM}AYQ}=I9HõÍ±½Ýq¸ˆ(€€€€€€€€ˆÀ¸ÈÔÍ±…¬€¡5P¥q¸ˆ(€€€€€€€€‰Ñ¹Ì€À¸Áq¸ˆ(€€€€€€€€‰!A!MQUM}AYQ}=9ôÅq¸ˆ°(€€€€€€€€ˆˆ°(€€€€€€€•áÁ•Ñ•‘}±…‰•°ô‰Í±½Üˆ°(€€€€¤(€€€…ÍÍ•ÉÐÙ…±Õ•l‰Ý½ÉÍÑ}Í•ÑÕÁ}Í±…­}¹Ì‰t€ôô€À¸ÈÔ(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰Í¥¸…¹ÍÑ…ÑÕÌˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Á…ÉÍ•}½Á•¹ÍÑ…}½ÕÑÁÕÐ (€€€€€€€€€€€€‰!A!MQUM}AYQ}=I9HõÍ±½Ýq¸ˆ(€€€€€€€€€€€€ˆ´À¸ÈÔÍ±…¬€¡5P¥q¸ˆ(€€€€€€€€€€€€‰Ñ¹Ì€´Ä¸Áq¸ˆ(€€€€€€€€€€€€‰!A!MQUM}AYQ}=9ôÅq¸ˆ°(€€€€€€€€€€€€ˆˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}±…‰•°ô‰Í±½Üˆ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}Á…ÉÍ•}½Á•¹ÍÑ…}½ÕÑÁÕÑ}É•©•ÑÍ}™…Ñ…±}‘¥…¹½ÍÑ¥Ì ¤€´ø9½¹”è(€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰™…Ñ…°‘¥…¹½ÍÑ¥Œˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Á…ÉÍ•}½Á•¹ÍÑ…}½ÕÑÁÕÐ (€€€€€€€€€€€€‰!A!MQUM}AYQ}=I9HõÑåÁq¸ˆ(€€€€€€€€€€€€ˆÀ¸ÔÍ±…¬€¡5P¥q¸ˆ(€€€€€€€€€€€€‰Ñ¹Ì€À¸Áq¸ˆ(€€€€€€€€€€€€‰!A!MQUM}AYQ}=9ôÅq¸ˆ°(€€€€€€€€€€€€‰ÉÉ½ÈèÉ•…‘}ÍÁ•˜™…¥±•‘q¸ˆ°(€€€€€€€€€€€•áÁ•Ñ•‘}±…‰•°ô‰ÑåÀˆ°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}•¹‘}Ñ½}•¹‘}‰½½ÑÍÑÉ…Á}É•™•É•¹•}…¹‘}ÍÑÉ¥Ñ}ÅÕ…±¥™¥…Ñ¥½¸ (€€€ÑµÁ}Á…Ñ èA…Ñ °(¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€‰½½ÑÍÑÉ…Á}½ÕÐ€ôÑµÁ}Á…Ñ €¼€‰‰½½ÑÍÑÉ…Àˆ(€€€‰½½ÑÍÑÉ…À€ôÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€‰½½ÑÍÑÉ…Á}½ÕÐ°(€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€ÕÁÍÑÉ•…µ}ÉÕ¹}¥ôˆÄÈÌˆ°(€€€€¤(€€€…ÍÍ•ÉÐ‰½½ÑÍÑÉ…Ál‰±…¥µÌ‰ul‰…±±|ÌÙ}Á½Í¥Ñ¥Ù•}…¹…±åÍ•Í}½µÁ±•Ñ•‰t¥ÌQÉÕ”(€€€…ÍÍ•ÉÐ‰½½ÑÍÑÉ…Ál‰±…¥µÌ‰ul‰½µÁ…É…Ñ¥Ù•}ÁÙÑ}±…¥µ}•¹…‰±•‰t¥Ì…±Í”(€€€…ÍÍ•ÉÐ‰½½ÑÍÑÉ…Ál‰É•É•ÍÍ¥½¸‰ul‰‰½½ÑÍÑÉ…Á}É•™•É•¹•}É•ÅÕ¥É•‰t¥ÌQÉÕ”(€€€…ÍÍ•ÉÐÍÕ´ (€€€€€€€±•¸¡…Í•l‰½É¹•ÉÌ‰um½É¹•Éul‰É•Á±…åÌ‰t¤(€€€€€€€™½È‰…­•¹¥¸‰½½ÑÍÑÉ…Ál‰‰…­•¹‘Ì‰t¹Ù…±Õ•Ì ¤(€€€€€€€™½È…Í”¥¸‰…­•¹‘l‰Á¡åÍ¥…±}…ÑÑ•µÁÑÌ‰t¹Ù…±Õ•Ì ¤(€€€€€€€™½È½É¹•È¥¸=I9IL(€€€€¤€ôô€ÌØ((€€€É•™•É•¹”€ôÑµÁ}Á…Ñ €¼€‰É•™•É•¹”¹©Í½¸ˆ(€€€ÁÙÑ}½É¹•È¹‰Õ¥±‘}É•™•É•¹” (€€€€€€€‰½½ÑÍÑÉ…Á}½ÕÐ€¼€‰ÁÙÑ}½É¹•É}•Ù¥‘•¹”¹©Í½¸ˆ°(€€€€€€€É•™•É•¹”°(€€€€¤(€€€™¥¹…±}½ÕÐ€ôÑµÁ}Á…Ñ €¼€‰™¥¹…°ˆ(€€€™¥¹…°€ôÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€™¥¹…±}½ÕÐ°(€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€É•™•É•¹•}Á…Ñ õÉ•™•É•¹”°(€€€€€€€ÕÁÍÑÉ•…µ}ÉÕ¹}¥ôˆÄÈÐˆ°(€€€€¤(€€€…ÍÍ•ÉÐ™¥¹…±l‰É•É•ÍÍ¥½¸‰ul‰Á…ÍÍ•‰t¥ÌQÉÕ”(€€€…ÍÍ•ÉÐ™¥¹…±l‰±…¥µÌ‰ul‰½µÁ…É…Ñ¥Ù•}ÁÙÑ}±…¥µ}•¹…‰±•‰t¥ÌQÉÕ”(€€€…ÍÍ•ÉÐ™¥¹…±l‰±…¥µÌ‰ul‰™½Õ¹‘Éå}Í¥¹½™™}ÍÑ…}Á•É™½Éµ•‰t¥Ì…±Í”(€€€…ÍÍ•ÉÐ™¥¹…±l‰•á•ÕÑ¥½¸‰ul‰ÕÁÍÑÉ•…µ}Á¡åÍ¥…±}Ý½É­™±½Ý}ÉÕ¹}¥‰t€ôô€ˆÄÈÐˆ(€€€…ÍÍ•ÉÐÁÙÑ}½É¹•È¹Ù…±¥‘…Ñ•}•á¥ÍÑ¥¹}É•™•É•¹” (€€€€€€€™¥¹…±}½ÕÐ€¼€‰ÁÙÑ}½É¹•É}•Ù¥‘•¹”¹©Í½¸ˆ°(€€€€€€€É•™•É•¹”°(€€€€¥l‰Á…ÍÍ•‰t¥ÌQÉÕ”(()‘•˜Ñ•ÍÑ}É•™•É•¹•}É•©•ÑÍ}µ•ÑÉ¥}‘É¥™Ð¡ÑµÁ}Á…Ñ èA…Ñ ¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€½ÕÑÁÕÐ€ôÑµÁ}Á…Ñ €¼€‰‰½½ÑÍÑÉ…Àˆ(€€€ÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€½ÕÑÁÕÐ°(€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€¤(€€€•Ù¥‘•¹•}Á…Ñ €ô½ÕÑÁÕÐ€¼€‰ÁÙÑ}½É¹•É}•Ù¥‘•¹”¹©Í½¸ˆ(€€€É•™•É•¹•}Á…Ñ €ôÑµÁ}Á…Ñ €¼€‰É•™•É•¹”¹©Í½¸ˆ(€€€ÁÙÑ}½É¹•È¹‰Õ¥±‘}É•™•É•¹”¡•Ù¥‘•¹•}Á…Ñ °É•™•É•¹•}Á…Ñ ¤(€€€•Ù¥‘•¹”€ô©Í½¸¹±½…‘Ì¡•Ù¥‘•¹•}Á…Ñ ¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤(€€€•Ù¥‘•¹•l‰‰…­•¹‘Ì‰ul‰Í¡…É•‘}‘…œ‰ul‰Á¡åÍ¥…±}…ÑÑ•µÁÑÌ‰ulˆÄ‰ul‰½É¹•ÉÌ‰ul(€€€€€€€€‰Í±½Üˆ(€€€ul‰µ•ÑÉ¥Ì‰ul‰Ý½ÉÍÑ}Í•ÑÕÁ}Í±…­}¹Ì‰t€ô€´ää¸À(€€€‘É¥™Ñ•€ôÑµÁ}Á…Ñ €¼€‰‘É¥™Ñ•¹©Í½¸ˆ(€€€}ÝÉ¥Ñ•}©Í½¸¡‘É¥™Ñ•°•Ù¥‘•¹”¤((€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰ÁÉ½©•Ñ¥½¸‘¥™™•ÉÌˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Ù…±¥‘…Ñ•}•á¥ÍÑ¥¹}É•™•É•¹”¡‘É¥™Ñ•°É•™•É•¹•}Á…Ñ ¤(()‘•˜Ñ•ÍÑ}‰Õ¥±‘•É}É•©•ÑÍ}…}Íåµ±¥¹­•‘}Á¡åÍ¥…±}É½½Ð¡ÑµÁ}Á…Ñ èA…Ñ ¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€±¥¹¬€ôÑµÁ}Á…Ñ €¼€‰Á¡åÍ¥…°µ±¥¹¬ˆ(€€€±¥¹¬¹Íåµ±¥¹­}Ñ¼¡Ù…±Õ•Íl‰Á¡åÍ¥…°‰t¹¹…µ”°Ñ…É•Ñ}¥Í}‘¥É•Ñ½ÉäõQÉÕ”¤((€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰Íåµ±¥¹­Ìˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€€€€€±¥¹¬°(€€€€€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€€€€€ÑµÁ}Á…Ñ €¼€‰½ÕÐˆ°(€€€€€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}Í½ÕÉ•}¡…¥¹}É•©•ÑÍ}Á½ÍÑ}Á¡åÍ¥…±}µ…¹¥™•ÍÑ}‰¥¹‘¥¹}‘É¥™Ð (€€€ÑµÁ}Á…Ñ èA…Ñ °(¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€Á½ÍÑ}Á…Ñ €ôÙ…±Õ•Íl‰Á½ÍÐ‰t€¼€‰Á½ÍÑ}Á¡åÍ¥…±}•ÅÕ¥Ù…±•¹•}•Ù¥‘•¹”¹©Í½¸ˆ(€€€Á½ÍÐ€ô©Í½¸¹±½…‘Ì¡Á½ÍÑ}Á…Ñ ¹É•…‘}Ñ•áÐ¡•¹½‘¥¹œô‰ÕÑ˜´àˆ¤¤(€€€Á½ÍÑl‰‰…­•¹‘Ì‰ul‰Í¡…É•‘}‘…œ‰ul‰…ÑÑ•µÁÑÌ‰ulÁul‰Á¡åÍ¥…±}ÉÕ¹}µ…¹¥™•ÍÐ‰ul(€€€€€€€€‰Í¡„ÈÔØˆ(€€€t€ô€ˆÀˆ€¨€ØÐ(€€€}ÝÉ¥Ñ•}©Í½¸¡Á½ÍÑ}Á…Ñ °Á½ÍÐ¤((€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰µ…¹¥™•ÍÐ‰¥¹‘¥¹œ‘¥™™•ÉÌˆ¤è(€€€€€€€ÁÙÑ}½É¹•È¹Ù…±¥‘…Ñ•}Í½ÕÉ•}¡…¥¸ (€€€€€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€€¤(()‘•˜Ñ•ÍÑ}É…Ý}É•Á½ÉÑ}É•Á±…å}‘•Ñ•ÑÍ}Ñ…µÁ•É¥¹œ¡ÑµÁ}Á…Ñ èA…Ñ ¤€´ø9½¹”è(€€€•á•ÕÑ…‰±”°|€ô}µ…­•}½Á•¹ÍÑ„¡ÑµÁ}Á…Ñ °€‰„ˆ€¨€ÐÀ¤(€€€Ý½É­‘¥È€ôÑµÁ}Á…Ñ €¼€‰ÉÕ¸ˆ(€€€É•½É€ô}½Á•¹ÍÑ„¹ÉÕ¹}½Á•¹ÍÑ„ (€€€€€€€•á•ÕÑ…‰±”õ•á•ÕÑ…‰±”°(€€€€€€€Ý½É­‘¥ÈõÝ½É­‘¥È°(€€€€€€€ÍÉ¥ÁÐôÁÕÑÌ€‰!A!MQUM}AYQ}=I9HõÍ±½Ü‰q¸œ°(€€€€€€€±…‰•°ô‰Í±½Üˆ°(€€€€€€€É•Á±…äôÄ°(€€€€€€€Ñ¥µ•½ÕÑ}Í•½¹‘ÌôÌÀ°(€€€€¤(€€€€¡Ý½É­‘¥È€¼€‰ÍÑ‘½ÕÐ¹ÑáÐˆ¤¹ÝÉ¥Ñ•}Ñ•áÐ ‰Ñ…µÁ•É•‘q¸ˆ°•¹½‘¥¹œô‰ÕÑ˜´àˆ¤((€€€Ý¥Ñ ÁåÑ•ÍÐ¹É…¥Í•Ì¡ÁÙÑ}½É¹•È¹AYQ½É¹•ÉÉÉ½È°µ…Ñ ô‰‘¥•ÍÐ¡…¹•ˆ¤è(€€€€€€€}½Á•¹ÍÑ„¹É•Á±…å}ÉÕ¸¡Ý½É­‘¥È°É•½É°•áÁ•Ñ•‘}±…‰•°ô‰Í±½Üˆ¤(()‘•˜Ñ•ÍÑ}ÍÑ…‰±•}ÁÉ½©•Ñ¥½¹}•á±Õ‘•Í}ÉÕ¹}¥‘Ì¡ÑµÁ}Á…Ñ èA…Ñ ¤€´ø9½¹”è(€€€Ù…±Õ•Ì€ô}™¥áÑÕÉ”¡ÑµÁ}Á…Ñ ¤(€€€™¥ÉÍÑ}½ÕÐ€ôÑµÁ}Á…Ñ €¼€‰™¥ÉÍÐˆ(€€€Í•½¹‘}½ÕÐ€ôÑµÁ}Á…Ñ €¼€‰Í•½¹ˆ(€€€™¥ÉÍÐ€ôÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€™¥ÉÍÑ}½ÕÐ°(€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€ÕÁÍÑÉ•…µ}ÉÕ¹}¥ôˆÄÄÄˆ°(€€€€¤(€€€Í•½¹€ôÁÙÑ}½É¹•È¹‰Õ¥±‘}•Ù¥‘•¹” (€€€€€€€Ù…±Õ•Íl‰Á¡åÍ¥…°‰t°(€€€€€€€Ù…±Õ•Íl‰Á½ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰Á‘¬‰t°(€€€€€€€Ù…±Õ•Íl‰½Á•¹ÍÑ„‰t°(€€€€€€€Ù…±Õ•Íl‰Ñ½½±}µ…¹¥™•ÍÐ‰t°(€€€€€€€Ù…±Õ•Íl‰½¹ÑÉ…Ð‰t°(€€€€€€€Í•½¹‘}½ÕÐ°(€€€€€€€Í½ÕÉ•}É•Ù¥Í¥½¸õIY%M%=8°(€€€€€€€ÕÁÍÑÉ•…µ}ÉÕ¹}¥ôˆÈÈÈˆ°(€€€€¤((€€€…ÍÍ•ÉÐ}É•™•É•¹”¹ÍÑ…‰±•}ÁÉ½©•Ñ¥½¸¡™¥ÉÍÐ¤€ôô}É•™•É•¹”¹ÍÑ…‰±•}ÁÉ½©•Ñ¥½¸¡Í•½¹¤