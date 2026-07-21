#!/usr/bin/env python3
"""Group→role and group→team mappings, plus the bootstrap-admin rule.

Federated identities arrive carrying IdP group names; these ordered rule sets
translate those groups into Mewbo roles and team slugs at login/provision time.
Rules own their own matching (exact or pre-compiled regex, case-insensitive) —
there is no service-side match dispatch. The ``BootstrapRule`` is the escape
hatch that seeds the first admin before any role has been assigned.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Literal

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator


class MappingRule(BaseModel):
    """One ordered rule: a group ``match`` → a ``target`` role/team slug.

    ``match_kind="regex"`` pre-compiles the pattern once at validation (case-
    insensitive, full-string) so a bad pattern is a definition-time error and
    matching stays allocation-free; ``exact`` is a case-insensitive equality.
    """

    model_config = ConfigDict(extra="forbid")

    match: str
    match_kind: Literal["exact", "regex"] = "exact"
    target: str

    _pattern: re.Pattern[str] | None = PrivateAttr(default=None)

    @model_validator(mode="after")
    def _compile(self) -> MappingRule:
        if self.match_kind == "regex":
            try:
                self._pattern = re.compile(self.match, re.IGNORECASE)
            except re.error as exc:
                raise ValueError(f"invalid regex {self.match!r}: {exc}") from exc
        return self

    def matches(self, group: str) -> bool:
        """Whether an IdP ``group`` name satisfies this rule."""
        if self.match_kind == "regex":
            return self._pattern is not None and self._pattern.fullmatch(group) is not None
        return group.casefold() == self.match.casefold()


class _GroupMapping(BaseModel):
    """Shared ordered-rule matching for the role and team mappings."""

    model_config = ConfigDict(extra="forbid")

    rules: tuple[MappingRule, ...] = ()

    def _match_all(self, groups: Sequence[str]) -> list[str]:
        """Targets of every rule that matches any group, in rule order, deduped."""
        matched: list[str] = []
        for rule in self.rules:
            if any(rule.matches(group) for group in groups) and rule.target not in matched:
                matched.append(rule.target)
        return matched


class GroupRoleMapping(_GroupMapping):
    """Ordered group→role rules, with a default role when nothing matches.

    ``resolve`` never returns empty: an identity that matches no rule still gets
    ``default_role`` so every user lands with a defined, least-privilege role.
    """

    default_role: str = "viewer"

    def resolve(self, groups: Sequence[str]) -> tuple[str, ...]:
        """Roles for the given IdP groups (ordered, deduped; default when empty)."""
        matched = self._match_all(groups)
        if not matched:
            matched.append(self.default_role)
        return tuple(matched)


class GroupTeamMapping(_GroupMapping):
    """Ordered group→team-slug rules.

    Unlike roles there is no default: an unmatched identity simply joins no
    team, which is a valid state.
    """

    def resolve(self, groups: Sequence[str]) -> tuple[str, ...]:
        """Team slugs for the given IdP groups (ordered, deduped; may be empty)."""
        return tuple(self._match_all(groups))


class BootstrapRule(BaseModel):
    """Seeds the first admin from IdP groups or a fixed subject allowlist.

    This is the cold-start escape hatch: before anyone can be granted the admin
    role through the UI, an admin has to exist. Matching either an ``admin_group``
    (case-insensitive) or an explicit ``admin_subjects`` entry confers admin.
    """

    model_config = ConfigDict(extra="forbid")

    admin_group: str | None = None
    admin_subjects: tuple[str, ...] = ()

    def is_admin(self, external_groups: Sequence[str], subject: str) -> bool:
        """Whether ``subject`` (with its IdP ``external_groups``) bootstraps as admin."""
        if subject in self.admin_subjects:
            return True
        if self.admin_group is not None:
            target = self.admin_group.casefold()
            return any(group.casefold() == target for group in external_groups)
        return False


__all__ = [
    "MappingRule",
    "GroupRoleMapping",
    "GroupTeamMapping",
    "BootstrapRule",
]
