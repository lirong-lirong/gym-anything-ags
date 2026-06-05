"""Compatibility wrapper for the benchmark split registry.

Prefer importing from ``benchmarks.cua_world.registry`` or ``benchmarks.cua_world.registry.splits``.
"""

from .splits import load_environment_task_splits


ENV_TASK_SPLITS = load_environment_task_splits(surface="raw")
VERIFIED_ENV_TASK_SPLITS = load_environment_task_splits(surface="verified")
AGS_STABLE_ENV_TASK_SPLITS = load_environment_task_splits(surface="ags_stable")

__all__ = [
    "AGS_STABLE_ENV_TASK_SPLITS",
    "ENV_TASK_SPLITS",
    "VERIFIED_ENV_TASK_SPLITS",
    "load_environment_task_splits",
]
