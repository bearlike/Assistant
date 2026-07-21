#!/usr/bin/env python3
"""Deterministic demo database seeder for Mewbo (demo-as-code).

Public surface: the :class:`~mewbo_demo_seeder.models.SeedBundle` contract and
the :class:`~mewbo_demo_seeder.seeder.DemoSeeder` atomic class. The CLI entry
point lives in ``__main__`` (``python -m mewbo_demo_seeder``).
"""

from __future__ import annotations

from mewbo_demo_seeder.models import SeedBundle
from mewbo_demo_seeder.seeder import DemoSeeder, SeedReport

__all__ = ["SeedBundle", "DemoSeeder", "SeedReport"]
