#!/usr/bin/env python3
"""Validate a routed registered-tile smoke run against a pinned reference."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import struct
from pathlib import Path
from typing import Any

SHA256_RE = re.compile(r"[0-9a-f]{64}")
SECTION_COUNT_RE = re.compile(
    r"^(?P<section>COMPONENTS|NETS|SPECIALNETS|PINS|VIAS)\s+(?P<count>\d+)\s*;"
)
UNITS_RE = re.compile(r"^UNITS DISTANCE MICRONS (?P<value>\d+)\s*;")
DIE_RE = re.compile(
    r"^DIEAREA \( (?P<x0>-?\d+) (?P<y0>-?\d+) \) "
    r"\( (?P<x1>-?\d+) (?P<y1>-?\d+) \)\s*;"
)
DATE_RE = re.compile(r'^\*DATE ".*"$', re.MULTILINE)


class SmokeValidationError(RuntimeError):
    """Raised when the smoke run differs from its pinned reference."""


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SmokeValidationError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SmokeValidationError(f"JSON root must be an object: {path}")
    return value


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def require_sha256(value: Any, *, context: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise SmokeValidationError(f"{context} is not a lowercase SHA-256 digest")
    return value


def exactly_one(root: Path, pattern: str) -> Path:
    matches = sorted(root.glob(pattern))
    if len(matches) != 1:
        raise SmokeValidationError(
            f"expected exactly one {pattern!r}, found {len(matches)}: {matches}"
        )
    path = matches[0]
    if not path.is_file() or path.stat().st_size == 0:
        raise SmokeValidationError(f"required artifact is missing or empty: {path}")
    if path.is_symlink():
        raise SmokeValidationError(f"required artifact must not be a symlink: {path}")
    return path


def parse_def(path: Path) -> dict[str, Any]:
    units: int | None = None
    die_area_dbu: list[int] | None = None
    counts: dict[str, int] = {}
    row_count = 0
    track_count = 0

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("ROW "):
            row_count += 1
        elif line.startswith("TRACKS "):
            track_count += 1

        units_match = UNITS_RE.fullmatch(line)
        if units_match is not None:
            units = int(units_match.group("value"))
            continue

        die_match = DIE_RE.fullmatch(line)
        if die_match is not None:
            die_area_dbu = [
                int(die_match.group("x0")),
                int(die_match.group("y0")),
                int(die_match.group("x1")),
                int(die_match.group("y1")),
            ]
            continue

        section_match = SECTION_COUNT_RE.fullmatch(line)
        if section_match is not None:
            counts[section_match.group("section")] = int(section_match.group("count"))

    if units is None or units <= 0:
        raise SmokeValidationError("DEF does not contain valid database units")
    if die_area_dbu is None:
        raise SmokeValidationError("DEF does not contain one DIEAREA statement")
    required_sections = {"COMPONENTS", "NETS", "SPECIALNETS", "PINS", "VIAS"}
    if set(counts) != required_sections:
        raise SmokeValidationError(
            f"DEF section counts are incomplete: expected {required_sections}, got {set(counts)}"
        )

    return {
        "database_units_per_micron": units,
        "die_area_dbu": die_area_dbu,
        "die_area_um": [value / units for value in die_area_dbu],
        "component_count": counts["COMPONENTS"],
        "net_count": counts["NETS"],
        "special_net_count": counts["SPECIALNETS"],
        "pin_count": counts["PINS"],
        "via_definition_count": counts["VIAS"],
        "row_statement_count": row_count,
        "track_statement_count": track_count,
    }


def normalize_gds_timestamps(raw: bytes) -> tuple[bytes, int]:
    output = bytearray(raw)
    offset = 0
    normalized = 0

    while offset < len(raw):
        if offset + 4 > len(raw):
            raise SmokeValidationError("truncated GDS record header")
        record_length = struct.unpack(">H", raw[offset : offset + 2])[0]
        if record_length < 4 or record_length % 2 != 0:
            raise SmokeValidationError(
                f"invalid GDS record length {record_length} at offset {offset}"
            )
        end = offset + record_length
        if end > len(raw):
            raise SmokeValidationError("truncated GDS record payload")
        record_type = raw[offset + 2]
        if record_type in (0x01, 0x05):
            if record_length != 28:
                raise SmokeValidationError(f"unexpected timestamp record length {record_length}")
            output[offset + 4 : end] = b"\x00" * (record_length - 4)
            normalized += 1
        offset = end

    if offset != len(raw):
        raise SmokeValidationError("GDS parser did not consume the complete file")
    if normalized == 0:
        raise SmokeValidationError("GDS contains no BGNLIB/BGNSTR timestamps")
    return bytes(output), normalized


def normalize_spef_date(raw: bytes) -> tuple[bytes, int]:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmokeValidationError("SPEF is not valid UTF-8") from exc
    matches = DATE_RE.findall(text)
    if len(matches) != 1:
        raise SmokeValidationError(f"expected one SPEF *DATE record, found {len(matches)}")
    normalized = DATE_RE.sub('*DATE "<normalized>"', text, count=1)
    return normalized.encode("utf-8"), 1


def require_exact_artifact(
    path: Path,
    expected: dict[str, Any],
    *,
    context: str,
) -> dict[str, Any]:
    expected_size = expected.get("size_bytes")
    if type(expected_size) is not int or expected_size <= 0:
        raise SmokeValidationError(f"{context}.size_bytes is invalid")
    expected_digest = require_sha256(expected.get("sha256"), context=context)
    actual = {
        "path": path.as_posix(),
        "size_bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }
    if actual["size_bytes"] != expected_size:
        raise SmokeValidationError(
            f"{context} size changed: expected {expected_size}, got {actual['size_bytes']}"
        )
    if actual["sha256"] != expected_digest:
        raise SmokeValidationError(
            f"{context} digest changed: expected {expected_digest}, got {actual['sha256']}"
        )
    return actual


def compare_metrics(actual: dict[str, Any], expected: dict[str, Any]) -> None:
    if set(actual) != set(expected):
        raise SmokeValidationError(
            f"metric key set changed: expected {sorted(expected)}, got {sorted(actual)}"
        )
    for name, expected_value in expected.items():
        actual_value = actual[name]
        if type(expected_value) not in (int, float):
            raise SmokeValidationError(f"reference metric {name} is not numeric")
        if type(actual_value) not in (int, float):
            raise SmokeValidationError(f"observed metric {name} is not numer²È="25¹‘•™•É•¹”¹•Ð ‰½É¥¥¹…±}¹•Ñ±¥ÍÐˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰½É¥¥¹…°Íå¹Ñ¡•Í¥é•µ¹•Ñ±¥ÍÐÍ¥¹…ÑÕÉ”¡…¹•ˆ¤(€€€¥˜½µÁ…Ñ¥‰¥±¥Ñä¹•Ð ‰Í…¹¥Ñ¥é•ˆ¤€„ô½µÁ…Ñ¥‰¥±¥Ñå}É•™•É•¹”¹•Ð ‰Í…¹¥Ñ¥é•‘}¹•Ñ±¥ÍÐˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰Í…¹¥Ñ¥é•Íå¹Ñ¡•Í¥é•µ¹•Ñ±¥ÍÐÍ¥¹…ÑÕÉ”¡…¹•ˆ¤(€€€¥˜Í¡„ÈÔÙ}™¥±”¡½µÁ…Ñ¥‰¥±¥Ñå}Á…Ñ ¤€„ô½µÁ…Ñ¥‰¥±¥Ñå}É•™•É•¹”¹•Ð ‰µ…¹¥™•ÍÑ}Í¡„ÈÔØˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰½µÁ…Ñ¥‰¥±¥Ñäµ…¹¥™•ÍÐ‘¥•ÍÐ¡…¹•ˆ¤((€€€½ÕÑÁÕÑÌ€ôì(€€€€€€€€‰™¥¹…±}‘Ìˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹‘Ìˆ¤°(€€€€€€€€‰™¥¹…±}‘•˜ˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹‘•˜ˆ¤°(€€€€€€€€‰™¥¹…±}½Á•¹}‘ˆˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹½‘ˆˆ¤°(€€€€€€€€‰™¥¹…±}Ù•É¥±½œˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹Øˆ¤°(€€€€€€€€‰™¥¹…±}ÍÁ•˜ˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹ÍÁ•˜ˆ¤°(€€€€€€€€‰™¥¹…±}Í‘Œˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”¼Ù}™¥¹…°¹Í‘Œˆ¤°(€€€€€€€€‰É½ÕÑ•}Õ¥‘”ˆè•á…Ñ±å}½¹”¡É½½Ð°€‰É•ÍÕ±ÑÌ¼¨¨½Íµ½­”½É½ÕÑ”¹Õ¥‘”ˆ¤°(€€€ô(€€€…ÉÑ¥™…Ñ}É•™•É•¹”€ôÉ•™•É•¹”¹•Ð ‰…ÉÑ¥™…ÑÌˆ¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡…ÉÑ¥™…Ñ}É•™•É•¹”°‘¥Ð¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”…ÉÑ¥™…ÑÌ…É”µ…±™½Éµ•ˆ¤((€€€•á…Ñ}½ÕÑÁÕÑ}¹…µ•Ì€ô€ ‰™¥¹…±}‘•˜ˆ°€‰™¥¹…±}Ù•É¥±½œˆ°€‰™¥¹…±}Í‘Œˆ°€‰É½ÕÑ•}Õ¥‘”ˆ¤(€€€½‰Í•ÉÙ•‘}½ÕÑÁÕÑÌè‘¥ÑmÍÑÈ°‘¥ÑmÍÑÈ°¹åut€ôíô(€€€™½È¹…µ”¥¸•á…Ñ}½ÕÑÁÕÑ}¹…µ•Ìè(€€€€€€€•áÁ•Ñ•€ô…ÉÑ¥™…Ñ}É•™•É•¹”¹•Ð¡¹…µ”¤(€€€€€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡•áÁ•Ñ•°‘¥Ð¤è(€€€€€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È¡˜‰É•™•É•¹”…ÉÑ¥™…Ðí¹…µ•ô¥Ìµ…±™½Éµ•ˆ¤(€€€€€€€½‰Í•ÉÙ•‘}½ÕÑÁÕÑÍm¹…µ•t€ôÉ•ÅÕ¥É•}•á…Ñ}…ÉÑ¥™…Ð (€€€€€€€€€€€½ÕÑÁÕÑÍm¹…µ•t°(€€€€€€€€€€€•áÁ•Ñ•°(€€€€€€€€€€€½¹Ñ•áÐõ˜‰…ÉÑ¥™…ÑÌ¹í¹…µ•ôˆ°(€€€€€€€€¤((€€€™½È¹…µ”¥¸€ ‰™¥¹…±}‘Ìˆ°€‰™¥¹…±}½Á•¹}‘ˆˆ°€‰™¥¹…±}ÍÁ•˜ˆ¤è(€€€€€€€Á…Ñ €ô½ÕÑÁÕÑÍm¹…µ•t(€€€€€€€½‰Í•ÉÙ•‘}½ÕÑÁÕÑÍm¹…µ•t€ôì(€€€€€€€€€€€€‰Á…Ñ ˆèÁ…Ñ ¹…Í}Á½Í¥à ¤°(€€€€€€€€€€€€‰Í¥é•}‰åÑ•ÌˆèÁ…Ñ ¹ÍÑ…Ð ¤¹ÍÑ}Í¥é”°(€€€€€€€€€€€€‰Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡Á…Ñ ¤°(€€€€€€€ô((€€€ÍÑ…‰±•}É•™•É•¹”€ôÉ•™•É•¹”¹•Ð ‰ÍÑ…‰±•}Í¥¹…ÑÕÉ•Ìˆ¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡ÍÑ…‰±•}É•™•É•¹”°‘¥Ð¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”ÍÑ…‰±”Í¥¹…ÑÕÉ•Ì…É”µ…±™½Éµ•ˆ¤(€€€½‰Í•ÉÙ•‘}‘•˜€ôÁ…ÉÍ•}‘•˜¡½ÕÑÁÕÑÍl‰™¥¹…±}‘•˜‰t¤(€€€¥˜½‰Í•ÉÙ•‘}‘•˜€„ôÍÑ…‰±•}É•™•É•¹”¹•Ð ‰‘•˜ˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È (€€€€€€€€€€€€‰Á…ÉÍ•Í¥¹…ÑÕÉ”¡…¹•è€ˆ(€€€€€€€€€€€˜‰•áÁ•Ñ•íÍÑ…‰±•}É•™•É•¹”¹•Ð ‘•˜œ¥ô°½Ðí½‰Í•ÉÙ•‘}‘•™ôˆ(€€€€€€€€¤((€€€¹½Éµ…±¥é•‘}‘Ì°‘Í}½Õ¹Ð€ô¹½Éµ…±¥é•}‘Í}Ñ¥µ•ÍÑ…µÁÌ¡½ÕÑÁÕÑÍl‰™¥¹…±}‘Ì‰t¹É•…‘}‰åÑ•Ì ¤¤(€€€¹½Éµ…±¥é•‘}‘Í}‘¥•ÍÐ€ôÍ¡„ÈÔÙ}‰åÑ•Ì¡¹½Éµ…±¥é•‘}‘Ì¤(€€€¥˜¹½Éµ…±¥é•‘}‘Í}‘¥•ÍÐ€„ôÍÑ…‰±•}É•™•É•¹”¹•Ð ‰‘Í}Ñ¥µ•ÍÑ…µÁ}¹½Éµ…±¥é•‘}Í¡„ÈÔØˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰Ñ¥µ•ÍÑ…µÀµ¹½Éµ…±¥é•LÍ¥¹…ÑÕÉ”¡…¹•ˆ¤(€€€¥˜‘Í}½Õ¹Ð€„ôÍÑ…‰±•}É•™•É•¹”¹•Ð ‰‘Í}Ñ¥µ•ÍÑ…µÁ}É•½É‘Í}¹½Éµ…±¥é•ˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰LÑ¥µ•ÍÑ…µÀµÉ•½É½Õ¹Ð¡…¹•ˆ¤((€€€¹½Éµ…±¥é•‘}ÍÁ•˜°ÍÁ•™}½Õ¹Ð€ô¹½Éµ…±¥é•}ÍÁ•™}‘…Ñ”¡½ÕÑÁÕÑÍl‰™¥¹…±}ÍÁ•˜‰t¹É•…‘}‰åÑ•Ì ¤¤(€€€¹½Éµ…±¥é•‘}ÍÁ•™}‘¥•ÍÐ€ôÍ¡„ÈÔÙ}‰åÑ•Ì¡¹½Éµ…±¥é•‘}ÍÁ•˜¤(€€€¥˜¹½Éµ…±¥é•‘}ÍÁ•™}‘¥•ÍÐ€„ôÍÑ…‰±•}É•™•É•¹”¹•Ð ‰ÍÁ•™}‘…Ñ•}¹½Éµ…±¥é•‘}Í¡„ÈÔØˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰‘…Ñ”µ¹½Éµ…±¥é•MAÍ¥¹…ÑÕÉ”¡…¹•ˆ¤(€€€¥˜ÍÁ•™}½Õ¹Ð€„ôÍÑ…‰±•}É•™•É•¹”¹•Ð ‰ÍÁ•™}‘…Ñ•}É•½É‘Í}¹½Éµ…±¥é•ˆ¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰MA‘…Ñ”µÉ•½É½Õ¹Ð¡…¹•ˆ¤((€€€É•Á½ÉÑ}Á…Ñ €ô•á…Ñ±å}½¹”¡É½½Ð°€‰±½Ì¼¨¨½Íµ½­”¼Ù}É•Á½ÉÐ¹©Í½¸ˆ¤(€€€É•Á½ÉÐ€ô±½…‘}©Í½¸¡É•Á½ÉÑ}Á…Ñ ¤(€€€•áÁ•Ñ•‘}µ•ÑÉ¥Ì€ôÉ•™•É•¹”¹•Ð ‰µ•ÑÉ¥Ìˆ¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡•áÁ•Ñ•‘}µ•ÑÉ¥Ì°‘¥Ð¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”µ•ÑÉ¥Ì…É”µ…±™½Éµ•ˆ¤(€€€…ÑÕ…±}µ•ÑÉ¥Ì€ôí¹…µ”èÉ•Á½ÉÐ¹•Ð¡¹…µ”¤™½È¹…µ”¥¸•áÁ•Ñ•‘}µ•ÑÉ¥Íô(€€€½µÁ…É•}µ•ÑÉ¥Ì¡…ÑÕ…±}µ•ÑÉ¥Ì°•áÁ•Ñ•‘}µ•ÑÉ¥Ì¤((€€€±…¥µÌ€ôÉ•™•É•¹”¹•Ð ‰±…¥µÌˆ¤(€€€¥˜¹½Ð¥Í¥¹ÍÑ…¹”¡±…¥µÌ°‘¥Ð¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”±…¥µÌ…É”µ…±™½Éµ•ˆ¤(€€€É•ÅÕ¥É•‘}ÑÉÕ”€ô€ (€€€€€€€€‰É•¥ÍÑ•É•‘}Í½ÕÉ•}‰¥¹‘¥¹}Ù•É¥™¥•ˆ°(€€€€€€€€‰Í¥¹±•}‰…­•¹‘}½É™Í}™±½Ý}½µÁ±•Ñ•ˆ°(€€€€€€€€‰Á±…•µ•¹Ñ}Á•É™½Éµ•ˆ°(€€€€€€€€‰É½ÕÑ¥¹}Á•É™½Éµ•ˆ°(€€€€€€€€‰‘Í}•¹•É…Ñ•ˆ°(€€€€€€€€‰ÍÁ•™}•¹•É…Ñ•ˆ°(€€€€€€€€‰Ñ¥µ¥¹}½¹ÍÑÉ…¥¹ÑÍ}…¹…±åé•ˆ°(€€€€€€€€‰™½ÕÉ}¹…¹½Í•½¹‘}Ñ…É•Ñ}µ•Ñ}¥¹}ÅÕ…±¥™å¥¹}ÉÕ¸ˆ°(€€€€¤(€€€É•ÅÕ¥É•‘}™…±Í”€ô€ (€€€€€€€€‰µ…Ñ¡•‘}Ñ¡É••}‰…­•¹‘}Á¡åÍ¥…±}½µÁ…É¥Í½¹}Á•É™½Éµ•ˆ°(€€€€€€€€‰Á½ÍÑ}Á¡åÍ¥…±}•ÅÕ¥Ù…±•¹•}Ù•É¥™¥•ˆ°(€€€€€€€€‰‘É}±•…¸ˆ°(€€€€€€€€‰±ÙÍ}±•…¸ˆ°(€€€€€€€€‰Á½Ý•É}•ÍÑ¥µ…Ñ•‘}Ý¥Ñ¡}…Ñ¥Ù¥Ñäˆ°(€€€€€€€€‰Á½ÍÑ}±…å½ÕÑ}Á•á}Ù•É¥™¥•ˆ°(€€€€€€€€‰™½Õ¹‘Éå}Í¥¹½™™}½µÁ±•Ñ”ˆ°(€€€€€€€€‰Í¥±¥½¹}Ù•É¥™¥•ˆ°(€€€€¤(€€€¥˜…¹ä¡±…¥µÌ¹•Ð¡¹…µ”¤¥Ì¹½ÐQÉÕ”™½È¹…µ”¥¸É•ÅÕ¥É•‘}ÑÉÕ”¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”±…­Ì„É•ÅÕ¥É•Á½Í¥Ñ¥Ù”±…¥´ˆ¤(€€€¥˜…¹ä¡±…¥µÌ¹•Ð¡¹…µ”¤¥Ì¹½Ð…±Í”™½È¹…µ”¥¸É•ÅÕ¥É•‘}™…±Í”¤è(€€€€€€€É…¥Í”Mµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½È ‰É•™•É•¹”½Ù•ÉÍÑ…Ñ•Ì¥ÑÌ±…¥´‰½Õ¹‘…Éäˆ¤((€€€É•ÑÕÉ¸ì(€€€€€€€€‰Í¡•µ„ˆè€‰¡•Á¡…•ÍÑÕÌ¹½Á•¹É½…µÉ•¥ÍÑ•É•µÍµ½­”µÙ…±¥‘…Ñ¥½¸¹ØÄˆ°(€€€€€€€€‰É•™•É•¹•}¥ˆèÉ•™•É•¹•l‰É•™•É•¹•}¥‰t°(€€€€€€€€‰É•™•É•¹•}Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡É•™•É•¹•}Á…Ñ ¤°(€€€€€€€€‰Í½ÕÉ”ˆèÍ½ÕÉ•}½‰Í•ÉÙ•°(€€€€€€€€‰Ñ½½±¡…¥¸ˆèì(€€€€€€€€€€€€‰½É™Í}¥µ…•}É•Á½}‘¥•ÍÐˆè¥µ…•}É•˜°(€€€€€€€€€€€€‰½É™Í}¥µ…•}¥ˆè¥µ…•}¥°(€€€€€€€€€€€€‰Ñ½½±}Ù•ÉÍ¥½¹Í}Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡Ñ½½±}Ù•ÉÍ¥½¹Í}Á…Ñ ¤°(€€€€€€€ô°(€€€€€€€€‰½µÁ…Ñ¥‰¥±¥Ñå}ÑÉ…¹Í™½É´ˆèì(€€€€€€€€€€€€‰µ…¹¥™•ÍÑ}Í¡„ÈÔØˆèÍ¡„ÈÔÙ}™¥±”¡½µÁ…Ñ¥‰¥±¥Ñå}Á…Ñ ¤°(€€€€€€€€€€€€‰ÍÕ‰ÍÑ¥ÑÕÑ¥½¹}½Õ¹Ðˆè½µÁ…Ñ¥‰¥±¥Ñål‰ÍÕ‰ÍÑ¥ÑÕÑ¥½¹}½Õ¹Ð‰t°(€€€€€€€ô°(€€€€€€€€‰½ÕÑÁÕÑÌˆè½‰Í•ÉÙ•‘}½ÕÑÁÕÑÌ°(€€€€€€€€‰ÍÑ…‰±•}Í¥¹…ÑÕÉ•Ìˆèì(€€€€€€€€€€€€‰‘•˜ˆè½‰Í•ÉÙ•‘}‘•˜°(€€€€€€€€€€€€‰‘Í}Ñ¥µ•ÍÑ…µÁ}¹½Éµ…±¥é•‘}Í¡„ÈÔØˆè¹½Éµ…±¥é•‘}‘Í}‘¥•ÍÐ°(€€€€€€€€€€€€‰‘Í}Ñ¥µ•ÍÑ…µÁ}É•½É‘Í}¹½Éµ…±¥é•ˆè‘Í}½Õ¹Ð°(€€€€€€€€€€€€‰ÍÁ•™}‘…Ñ•}¹½Éµ…±¥é•‘}Í¡„ÈÔØˆè¹½Éµ…±¥é•‘}ÍÁ•™}‘¥•ÍÐ°(€€€€€€€€€€€€‰ÍÁ•™}‘…Ñ•}É•½É‘Í}¹½Éµ…±¥é•ˆèÍÁ•™}½Õ¹Ð°(€€€€€€€ô°(€€€€€€€€‰µ•ÑÉ¥Ìˆè…ÑÕ…±}µ•ÑÉ¥Ì°(€€€€€€€€‰±…¥µÌˆè±…¥µÌ°(€€€ô(()‘•˜Í•±™}Ñ•ÍÐ ¤€´ø9½¹”è(€€€ÍÁ•˜€ôˆœ©MA€‰¥••”€ÄÐàÄ´Ääää‰q¸©Q€‰Ñ½‘…ä‰q¸©M%8€‰à‰q¸œ(€€€¹½Éµ…±¥é•‘}ÍÁ•˜°½Õ¹Ð€ô¹½Éµ…±¥é•}ÍÁ•™}‘…Ñ”¡ÍÁ•˜¤(€€€…ÍÍ•ÉÐ½Õ¹Ð€ôô€Ä(€€€…ÍÍ•ÉÐˆÑ½‘…äœ¹½Ð¥¸¹½Éµ…±¥é•‘}ÍÁ•˜((€€€Ñ¥µ•ÍÑ…µÁ}Á…å±½…€ôˆ‰qàÀÀˆ€¨€ÈÐ(€€€‘Ì€ô€ (€€€€€€€ÍÑÉÕÐ¹Á…¬ ˆù!	ˆ°€Èà°€ÁàÀÄ°€ÁàÀÈ¤€¬Ñ¥µ•ÍÑ…µÁ}Á…å±½…€¬ÍÑÉÕÐ¹Á…¬ ˆù!	ˆ°€Ð°€ÁàÀÐ°€ÁàÀÀ¤(€€€€¤(€€€¹½Éµ…±¥é•‘}‘Ì°½Õ¹Ð€ô¹½Éµ…±¥é•}‘Í}Ñ¥µ•ÍÑ…µÁÌ¡‘Ì¤(€€€…ÍÍ•ÉÐ½Õ¹Ð€ôô€Ä(€€€…ÍÍ•ÉÐ±•¸¡¹½Éµ…±¥é•‘}‘Ì¤€ôô±•¸¡‘Ì¤((€€€ÑÉäè(€€€€€€€¹½Éµ…±¥é•}ÍÁ•™}‘…Ñ”¡ˆœ©MA€‰à‰q¸œ¤(€€€•á•ÁÐMµ½­•Y…±¥‘…Ñ¥½¹ÉÉ½Èè(€€€€€€€Á…ÍÌ(€€€•±Í”è(€€€€€€€É…¥Í”ÍÍ•ÉÑ¥½¹ÉÉ½È ‰MAÝ¥Ñ¡½ÕÐ€©QÝ…Ì…•ÁÑ•ˆ¤(()‘•˜Á…ÉÍ•}…ÉÌ ¤€´ø…ÉÁ…ÉÍ”¹9…µ•ÍÁ…”è(€€€Á…ÉÍ•È€ô…ÉÁ…ÉÍ”¹ÉÕµ•¹ÑA…ÉÍ•È ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÉ½½Ðˆ°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÉ•™•É•¹”ˆ°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÉ•¥ÍÑ•É•ˆ°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ¥µ…”µÉ•˜ˆ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µ½ÕÐˆ°ÑåÁ”õA…Ñ ¤(€€€Á…ÉÍ•È¹…‘‘}…ÉÕµ•¹Ð ˆ´µÍ•±˜µÑ•ÍÐˆ°…Ñ¥½¸ô‰ÍÑ½É•}ÑÉÕ”ˆ¤(€€€É•ÑÕÉ¸Á…ÉÍ•È¹Á…ÉÍ•}…ÉÌ ¤(()‘•˜µ…¥¸ ¤€´ø¥¹Ðè(€€€…ÉÌ€ôÁ…ÉÍ•}…ÉÌ ¤(€€€¥˜…ÉÌ¹Í•±™}Ñ•ÍÐè(€€€€€€€Í•±™}Ñ•ÍÐ ¤(€€€€€€€ÁÉ¥¹Ð ‰=Á•¹I=Íµ½­”µÉ•™•É•¹”Ù…±¥‘…Ñ½ÈÍ•±˜µÑ•ÍÐÁ…ÍÍ•¸ˆ¤(€€€€€€€É•ÑÕÉ¸€À(€€€É•ÅÕ¥É•€ô€¡…ÉÌ¹É½½Ð°…ÉÌ¹É•™•É•¹”°…ÉÌ¹É•¥ÍÑ•É•°…ÉÌ¹¥µ…•}É•˜°…ÉÌ¹½ÕÐ¤(€€€¥˜…¹ä¡Ù…±Õ”¥Ì9½¹”™½ÈÙ…±Õ”¥¸É•ÅÕ¥É•¤è(€€€€€€€É…¥Í”MåÍÑ•µá¥Ð ˆ´µÉ½½Ð°€´µÉ•™•É•¹”°€´µÉ•¥ÍÑ•É•°€´µ¥µ…”µÉ•˜°…¹€´µ½ÕÐ…É”É•ÅÕ¥É•ˆ¤(€€€É•ÍÕ±Ð€ôÙ…±¥‘…Ñ” (€€€€€€€…ÉÌ¹É½½Ð°(€€€€€€€…ÉÌ¹É•™•É•¹”°(€€€€€€€…ÉÌ¹É•¥ÍÑ•É•°(€€€€€€€…ÉÌ¹¥µ…•}É•˜°(€€€€¤(€€€…ÉÌ¹½ÕÐ¹Á…É•¹Ð¹µ­‘¥È¡Á…É•¹ÑÌõQÉÕ”°•á¥ÍÑ}½¬õQÉÕ”¤(€€€…ÉÌ¹½ÕÐ¹ÝÉ¥Ñ•}Ñ•áÐ (€€€€€€€©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ð°¥¹‘•¹ÐôÈ°Í½ÉÑ}­•åÌõQÉÕ”¤€¬€‰q¸ˆ°(€€€€€€€•¹½‘¥¹œô‰ÕÑ˜´àˆ°(€€€€¤(€€€ÁÉ¥¹Ð¡©Í½¸¹‘ÕµÁÌ¡É•ÍÕ±Ð°¥¹‘•¹ÐôÈ°Í½ÉÑ}­•åÌõQÉÕ”¤¤(€€€É•ÑÕÉ¸€À(()¥˜}}¹…µ•}|€ôô€‰}}µ…¥¹}|ˆè(€€€É…¥Í”MåÍÑ•µá¥Ð¡µ…¥¸ ¤¤