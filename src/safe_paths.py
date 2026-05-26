"""Path containment helpers.

A compromised or corrupted DB row could store an absolute or
``..``-escaping ``processed_audio_path``. Audio serving and cleanup
must never read or unlink anything outside ``settings.data_dir``.
"""

from pathlib import Path


class UnsafeRelativePath(ValueError):
    """Raised when a stored path tries to escape the data dir."""


def resolve_under_data_dir(data_dir: str | Path, relative_path: str) -> Path:
    """Resolve ``relative_path`` under ``data_dir`` or raise.

    Rejects absolute paths and any path that escapes the data dir
    after resolving (``..`` traversal, symlink-style breakouts).
    """
    if not relative_path:
        raise UnsafeRelativePath("empty path")
    rel = Path(relative_path)
    if rel.is_absolute():
        raise UnsafeRelativePath("absolute paths are not allowed")
    root = Path(data_dir).resolve()
    candidate = (root / rel).resolve()
    if not _is_relative_to(candidate, root):
        raise UnsafeRelativePath("path escapes data dir")
    return candidate


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False
