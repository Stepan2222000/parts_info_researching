"""Формат-валидация артикулов по правилам `brand_mapping.article_match_rules`.

Каноническую форму артикула в smart задают правила (regex + capture-группа), общие с
eBay-системами. МЫ НИЧЕГО НЕ КОНВЕРТИРУЕМ — только валидируем: артикул обязан УЖЕ быть
каноническим. Каждое правило: `find_regex` матчит грязную/каноническую форму, capture-группа
= каноника, незахваченное (дилерский префикс NN-, пробел) отбрасывается.

Гейтим по бренду запчасти (`brand_oem`): для артикула пробуем только правила его брендов
(`NN-` у Mercury — дилерский префикс, у BRP/Volvo/Yamaha — часть номера; без гейта поломаем).

Якорно (одна строка, а не текст): правило должно покрыть ВСЮ строку — `re.match` +
`m.end() == len(article)`. Вердикт:
  * capture == article  -> OK            (уже каноника)
  * capture != article  -> NOT_CANONICAL (доказанно грязно; expected = capture — что должна
                                          была выдать модель, напр. 26-8M0204670 -> 8M0204670)
  * ничто не покрыло    -> NO_RULE       (формат не описан правилами)

Правила читаются из БД (FDW `brand_mapping.article_match_rules`), кэш на процесс. Без скрытых
фолбеков: битый regex в правиле -> исключение наверх (правила правит человек в БД)."""

from __future__ import annotations

import re
from dataclasses import dataclass

OK = "ok"
NOT_CANONICAL = "not_canonical"
NO_RULE = "no_rule"

_RULES_SQL = (
    "SELECT name, canonical, find_regex, COALESCE(note, '') AS note "
    "FROM brand_mapping.article_match_rules WHERE enabled ORDER BY name"
)


@dataclass(frozen=True)
class Rule:
    name: str
    brand: str
    regex: "re.Pattern[str]"
    note: str


@dataclass(frozen=True)
class Verdict:
    status: str                 # OK | NOT_CANONICAL | NO_RULE
    expected: str | None        # каноника по правилу (для NOT_CANONICAL)
    rule_name: str | None       # какое правило сматчило (трассировка)

    @property
    def is_ok(self) -> bool:
        return self.status == OK


class RuleSet:
    """Скомпилированные правила, сгруппированные по бренду."""

    def __init__(self, rules: list[Rule]):
        self.rules = rules
        self.by_brand: dict[str, list[Rule]] = {}
        for r in rules:
            self.by_brand.setdefault(r.brand, []).append(r)

    @staticmethod
    def _canonical(m: "re.Match[str]") -> str:
        # Каноника = склейка непустых capture-групп по порядку; нет групп -> весь матч.
        groups = m.groups()
        return "".join(g for g in groups if g) if groups else m.group(0)

    def validate(self, article: str, brands) -> Verdict:
        """Вердикт по одному артикулу, гейт по брендам запчасти.

        OK выигрывает у NOT_CANONICAL: если хоть одно правило признаёт строку уже
        канонической — она валидна. NOT_CANONICAL — если правило свернуло строку к
        ДРУГОЙ канонике. Иначе NO_RULE."""
        if not article:
            return Verdict(NO_RULE, None, None)
        not_canon: Verdict | None = None
        for brand in brands:
            for rule in self.by_brand.get(brand, ()):
                m = rule.regex.match(article)
                if not m or m.end() != len(article):
                    continue  # правило не покрывает строку целиком
                canon = self._canonical(m)
                if canon == article:
                    return Verdict(OK, None, rule.name)
                if not_canon is None:
                    not_canon = Verdict(NOT_CANONICAL, canon, rule.name)
        return not_canon if not_canon is not None else Verdict(NO_RULE, None, None)

    def format_spec(self, brands=None) -> str:
        """Человекочитаемая спека форматов для промпта модели (из note правил).

        brands=None -> по всем брендам (бренд запчасти на старте ресерча ещё неизвестен)."""
        chosen = sorted(brands) if brands else sorted(self.by_brand)
        lines: list[str] = []
        for brand in chosen:
            rules = self.by_brand.get(brand, ())
            if not rules:
                continue
            notes = list(dict.fromkeys(r.note.strip() for r in rules if r.note.strip()))
            lines.append(f"- {brand}: " + " ".join(notes))
        return "\n".join(lines)


# ── кэш на процесс (правила меняются редко, правит человек в БД) ───────────────────
_cache: RuleSet | None = None


async def load_ruleset(conn, *, refresh: bool = False) -> RuleSet:
    """Грузит правила из FDW (conn — asyncpg Pool или Connection). Кэш на процесс."""
    global _cache
    if _cache is None or refresh:
        rows = await conn.fetch(_RULES_SQL)
        rules = [
            Rule(r["name"], r["canonical"], re.compile(r["find_regex"]), r["note"])
            for r in rows
        ]
        _cache = RuleSet(rules)
    return _cache
