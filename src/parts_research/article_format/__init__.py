"""Формат-валидация артикулов по правилам brand_mapping.article_match_rules."""

from .validator import (
    NO_RULE,
    NOT_CANONICAL,
    OK,
    RuleSet,
    Verdict,
    load_ruleset,
)

__all__ = ["NO_RULE", "NOT_CANONICAL", "OK", "RuleSet", "Verdict", "load_ruleset"]
