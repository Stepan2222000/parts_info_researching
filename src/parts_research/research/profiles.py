"""Профили этапов ресёрча: какие ОПЦИОНАЛЬНЫЕ этапы гнать в ране.

Ядро (main + kit_contents + валидации) не отключается: main создаёт базовый JSON,
а kit_contents обязателен, потому что «is_kit без состава» — критический фейл рана.
Порядок исполнения: main -> family_expansion -> low_confidence -> kit_contents ->
price_fallback -> difference -> phase2. Difference идёт ДО phase2; phase2 по
умолчанию выключен (пресет default).

Профиль фиксируется на ране (task_runs.profile) в канонической форме
{"preset": <имя|custom>, "stages": [<включённые опциональные этапы>]}.
NULL в БД = legacy-ран (до профилей): гнался полным пайплайном, при
reuse-проверках трактуется как full.

Ошибки не скрываем: неизвестный пресет/этап -> ValueError с перечнем допустимых."""

from __future__ import annotations

CORE_STAGES = ("main", "kit_contents")
# Порядок здесь = порядок исполнения в пайплайне.
OPTIONAL_STAGES = ("family_expansion", "low_confidence", "price_fallback", "difference", "phase2")
ALL_STAGES = ("main", "family_expansion", "low_confidence", "kit_contents",
              "price_fallback", "difference", "phase2")

PRESETS: dict[str, tuple[str, ...]] = {
    # только ядро: быстрый ран ~2-3 мин
    "fast": (),
    # всё, кроме phase2 (глобальный дефолт)
    "default": ("family_expansion", "low_confidence", "price_fallback", "difference"),
    # всё, включая агентский phase2
    "full": OPTIONAL_STAGES,
}

DEFAULT_PRESET = "default"


def resolve_profile(raw: str | dict | None) -> dict:
    """Вход API/CLI -> каноничный профиль {"preset", "stages"}.

    Принимает: None (дефолт), имя пресета строкой, {"preset": <имя>} либо
    {"stages": [<опциональные этапы>]}. Неизвестное имя/этап -> ValueError.
    """
    if raw is None:
        raw = DEFAULT_PRESET
    if isinstance(raw, str):
        raw = {"preset": raw}
    if not isinstance(raw, dict):
        raise ValueError(f"profile must be a preset name or object, got {type(raw).__name__}")

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
        return {"preset": preset, "stages": canonical}

    preset = raw.get("preset", DEFAULT_PRESET)
    if preset not in PRESETS:
        raise ValueError(f"unknown profile preset {preset!r}; allowed: {list(PRESETS)}")
    return {"preset": preset, "stages": list(PRESETS[preset])}


def full_profile() -> dict:
    return {"preset": "full", "stages": list(OPTIONAL_STAGES)}


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
