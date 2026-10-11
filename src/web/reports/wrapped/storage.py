from __future__ import annotations

from pathlib import Path


def wrapped_account_dir(account_dir: Path) -> Path:
    """Return the per-account wrapped working directory."""
    return account_dir / "_wrapped"


def wrapped_cache_dir(account_dir: Path) -> Path:
    d = wrapped_account_dir(account_dir) / "cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def wrapped_cache_path(
    *,
    account_dir: Path,
    scope: str,
    year: int,
    implemented_upto: int,
    options_tag: str | None = None,
) -> Path:
    suffix = f"_{options_tag}" if options_tag else ""
    return wrapped_cache_dir(account_dir) / f"{scope}_{year}_upto_{implemented_upto}{suffix}.json"
