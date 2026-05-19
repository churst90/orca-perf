"""Shared utility helpers for Orca.

Modules in this package are infrastructure primitives -- threading,
scheduling, etc. -- that get reused across the higher-level subsystems
(speech, braille, structural navigation, etc.). Each helper is self-
contained and free of dependencies on Orca's domain code so it can be
unit-tested in isolation.
"""
