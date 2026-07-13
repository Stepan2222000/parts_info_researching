"""Профили этапов ресёрча: какие ОПЦИОНАЛЬНЫЕ этапы гнать в ране.

Ядро (main + kit_contents + валидации) не отключается: main создаёт базовый JSON,
а kit_contents обязателен, потому что «is_kit без состава» — критический фейл рана.
Порядок исполнения: main -> family_expansion -> low_confidence -> kit_contents ->
part_of_kits -> price_fallback -> difference -> phase2. Difference идёт ДО phase2;
phase2 по умолчанию выключен (пресет default).

part_of_kits — «вверх»-поиск для НЕ-наборов (в какие наборы входит одиночная
деталь). Для наборов направление «вверх» ищется в ядровом kit_contents-этапе
(вместе с составом и под-наборами), поэтому им этот этап не нужен. По умолчанию
(default) выключен — «всем подряд не гоняем»; включается пресетом full или
кастомным списком stages.

Профиль фиксируется на ране (task_runs.profile) в канонической форме
{"preset": <имя|custom>, "stages": [<включённые опциональные этапы>],
"repair": <bool>}. repair — политика упавшей валидации этапа: true = вернуть
текст ошибки агенту на исправление (1 попытка, та же сессия), false = сразу
фейл (историческое поведение). Дефолт repair, когда submit не передал его
явно, — env RESEARCH_REPAIR_VALIDATION (settings.research_repair_validation).
NULL в БД = legacy-ран (до профилей): гнался полным пайплайном, при
reuse-проверках трактуется как full.

Ошибки не скрываем: неизвестный пресет/этап/не-bool repair -> ValueError."""

from __future__ import annotations

from ..config import settings

CORE_STAGES = ("main", "kit_contents")
# Порядок здесь = порядок исполнения в пайплайне.
OPTIONAL_STAGES = ("family_expansion", "low_confidence", "part_of_kits",
                   "price_fallback", "difference", "phase2")
ALL_STAGES = ("main", "family_expansion", "low_confidence", "kit_contents",
              "part_of_kits", "price_fallback", "difference", "phase2")

PRESETS: dict[str, tuple[str, ...]] = {
    # только ядро: быстрый ран ~2-3 мин
    "fast": (),
    # всё, кроме phase2 и part_of_kits (глобальный дефолт; «вверх»-поиск
    # одиночным деталям по умолчанию не гоняем)
    "default": ("family_expansion", "low_confidence", "price_fallback", "difference"),
    # всё, включая part_of_kits и агентский phase2
    "full": OPTIONAL_STAGES,
}

DEFAULT_PRESET = "default"


def _resolve_repair(raw: dict) -> bool:
    """Ключ repair из входа: явный bool либо env-дефолт. Не-bool -> ValueError."""
    if "repair" not in raw:
        return settings.research_repair_validation
    repair = raw["repair"]
    if not isinstance(repair, bool):
        raise ValueError(f"profile.repair must be a boolean, got {repair!r}")
    return repair


def resolve_profile(raw: str | dict | None) -> dict:
    """Вход API/CLI -> каноничный профиль {"preset", "stages", "repair"}.

    Принимает: None (дефолт), имя пресета строкой, {"preset": <имя>} либо
    {"stages": [<опциональные этапы>]}; опционально "repair": bool (без него —
    env-дефолт). Неизвестное имя/этап/не-bool repair -> ValueError.
    """
    if raw is None:
        raw = DEFAULT_PRESET
    if isinstance(raw, str):
        raw = {"preset": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"profile must be a preset name or object, got {type(raw).__name__}")

    repair = _resolve_repair(raw)

    if "stages" in raw:
        stages = raw["stages"]
        if not isinstance(stages, list) or not all(isinstance(s, str) for s in stages):
            raise ValueError("profile.stages must be a list of stage names")
        unknown = [s for s in stages if s not in OPTIONAL_STAGES]
        if unknown:
            raise ValueError(
                f"unknown optional stages: {unknown}; allowed: {list(OPTIONAL_STAGES)} "
                f"(core stages {list(CORE_STAGES)} always run and must not be listed)"
            )
        # канонический порядок — как в OPTIONAL_STAGES
        canonical = [s for s in OPTIONAL_STAGES if s in set(stages)]
        preset = next((name for name, ps in PRESETS.items() if list(ps) == canonical), "custom")
        return {"preset": preset, "stages": canonical, "repair": repair}

    preset = raw.get("preset", DEFAULT_PRESET)
    if preset not in PRESETS:
        raise ValueError(f"unknown profile preset {preset!r}; allowed: {list(PRESETS)}")
    return {"preset": preset, "stages": list(PRESETS[preset]), "repair": repair}


def full_profile() -> dict:
    return {"preset": "full", "stages": list(OPTIONAL_STAGES),
            "repair": settings.research_repair_validation}


def covers(existing: dict | None, requested: dict) -> bool:
    """Покрывает ли существующий профиль запрошенный (requested ⊆ existing).

    existing=None — legacy-ран до профилей: гнался полным пайплайном => full.
    """
    existing_stages = set((existing or full_profile())["stages"])
    return set(requested["stages"]) <= existing_stages


def stage_enabled(profile: dict, stage: str) -> bool:
    if stage in CORE_STAGES:
        return True
    if stage not in OPTIONAL_STAGES:
        raise ValueError(f"unknown stage {stage!r}")
    return stage in set(profile["stages"])
