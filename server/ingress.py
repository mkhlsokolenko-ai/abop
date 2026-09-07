"""Contract Ingress — приём handoff-бандла из LUDA (ADR-029, SDD §4).

Граница между продуктами (ADR-027): ABOP НЕ импортирует модели LUDA, а валидирует
бандл по wire-формату (dict из JSON). Схемы `luda.*/1.0` + мажор semver (ADR-025);
несовпадение мажора → отказ с явной ошибкой (SDD §4.3). На выходе — вердикт приёма
и `intake` (семя Project Graph: семья, навыки, потолок автономии, HITL, резидентность).

Инвариант ADR-013: автономия агента НЕ выше DeploymentContract.autonomy_level.
Здесь ingress лишь извлекает потолок; жёсткое ограничение применяет сборка/рантайм.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# Секция бандла → ожидаемое имя схемы (без версии). Версию проверяем по мажору.
SEAM_SCHEMAS = {
    "capability_request": "luda.capability_request",
    "deployment_contract": "luda.deployment_contract",
    "baseline_measurement": "luda.baseline_measurement",
}
SUPPORTED_MAJOR = 1  # ABOP этой версии умеет мажор 1 (ADR-025)

_AUTONOMY = {"A0", "A1", "A2", "A3", "A4"}
_EV_REF = re.compile(r"^ev:([0-9a-f]{10})$")  # ev:<первые 10 hex sha256>

_REQUIRED = {
    "capability_request": ("audit_id", "segment", "family", "skills"),
    "deployment_contract": ("audit_id", "segment", "autonomy_level", "limiting_axis"),
    "baseline_measurement": ("audit_id", "methodology_version", "metrics"),
}


@dataclass
class IngressResult:
    accepted: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    intake: dict[str, Any] | None = None  # семя Project Graph, если принято


def _parse_schema(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str) or "/" not in value:
        return None
    name, _, ver = value.partition("/")
    major = ver.split(".", 1)[0]
    if not major.isdigit():
        return None
    return name, int(major)


def _check_schema(errors: list[str], section: str, contract: dict) -> None:
    parsed = _parse_schema(contract.get("schema"))
    if parsed is None:
        errors.append(f"{section}: поле schema отсутствует или не version-строка")
        return
    name, major = parsed
    expected = SEAM_SCHEMAS[section]
    if name != expected:
        errors.append(f"{section}: чужая схема {name!r}, ожидалась {expected!r}")
    elif major != SUPPORTED_MAJOR:
        errors.append(
            f"{section}: мажор схемы {major} не поддержан (ABOP умеет {SUPPORTED_MAJOR}, ADR-025)"
        )


def _check_required(errors: list[str], section: str, contract: dict) -> None:
    for f in _REQUIRED[section]:
        if f not in contract or contract[f] in (None, ""):
            errors.append(f"{section}: обязательное поле {f!r} отсутствует")


def _collect_ev_refs(bundle: dict) -> set[str]:
    refs: set[str] = set()
    refs.update(r for r in bundle["capability_request"].get("evidence", []) if isinstance(r, str))
    for m in (bundle["baseline_measurement"].get("metrics") or {}).values():
        if isinstance(m, dict):
            refs.update(r for r in m.get("evidence", []) if isinstance(r, str))
    return refs


def validate(bundle: Any) -> IngressResult:
    """Проверка handoff-бандла LUDA. Возвращает вердикт приёма + intake."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(bundle, dict):
        return IngressResult(False, ["бандл не является объектом"])

    for section in SEAM_SCHEMAS:
        if not isinstance(bundle.get(section), dict):
            errors.append(f"{section}: секция отсутствует или не объект")
    if not isinstance(bundle.get("evidence_pack"), list):
        errors.append("evidence_pack: отсутствует или не список")
    if errors:
        return IngressResult(False, errors)

    for section in SEAM_SCHEMAS:
        _check_schema(errors, section, bundle[section])
        _check_required(errors, section, bundle[section])

    cap, dc, bm = (bundle["capability_request"], bundle["deployment_contract"],
                   bundle["baseline_measurement"])

    if dc.get("autonomy_level") not in _AUTONOMY:
        errors.append(f"deployment_contract: autonomy_level {dc.get('autonomy_level')!r} вне A0–A4")

    aids = {c.get("audit_id") for c in (cap, dc, bm)}
    if len(aids) > 1:
        errors.append(f"audit_id расходится между контрактами: {sorted(map(str, aids))}")

    prefixes = {s[:10] for s in bundle["evidence_pack"] if isinstance(s, str)}
    for ref in _collect_ev_refs(bundle):
        m = _EV_REF.match(ref)
        if not m:
            errors.append(f"evidence: ссылка {ref!r} не формата ev:<10hex>")
        elif m.group(1) not in prefixes:
            errors.append(f"evidence: {ref!r} не найдена в evidence_pack (провенанс неполон)")

    metrics = bm.get("metrics") or {}
    for name in (dc.get("acceptance") or {}):
        if name not in metrics:
            warnings.append(f"acceptance[{name}] нет в baseline.metrics — RunMetrics не с чем сравнить")

    if errors:
        return IngressResult(False, errors, warnings)

    intake = {
        "audit_id": cap["audit_id"],
        "segment": cap["segment"],
        "family": cap["family"],
        "skills": [s.get("id") for s in cap.get("skills", []) if isinstance(s, dict)],
        "modules": [
            {"id": m.get("id"), "access": m.get("access")}
            for m in cap.get("modules", []) if isinstance(m, dict)
        ],
        "autonomy_ceiling": dc["autonomy_level"],   # ADR-013: жёсткий потолок для сборки
        "limiting_axis": dc.get("limiting_axis"),
        "criticality": dc.get("criticality", "unknown"),
        "hitl_points": [h.get("id") for h in dc.get("hitl_points", []) if isinstance(h, dict)],
        "residency": dc.get("data_residency") or {},
        "acceptance": dict(dc.get("acceptance") or {}),
        "evidence_count": len(bundle["evidence_pack"]),
    }
    return IngressResult(True, errors, warnings, intake)
