"""Codex Kostyl — a desktop client for local AI coding agents."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("codex-kostyl")
except PackageNotFoundError:
    # Source-tree fallback for running without installing the package.
    __version__ = "0.1.0"
