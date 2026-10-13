"""Atomic file staging utilities for crash-safe writes."""

import os
from pathlib import Path
from typing import Callable, IO, Iterable


def staged_write(target: Path, mode: int, write: Callable[[IO[bytes]], None]) -> None:
    """
    Write to a target file atomically via a staging sibling.

    Prevents torn writes by writing to a <target>.new sibling first,
    then replacing the target with the staging file via os.replace.

    Args:
        target: The destination file path
        mode: File permissions to apply at creation (via os.open flags)
        write: Callable that receives a binary file object and writes data

    Raises:
        Any exception raised by write() or os.replace, after cleaning up
        the staging file.
    """
    # Staging file is a sibling with .new suffix
    staging = target.with_suffix(target.suffix + ".new")

    # Open staging file with the specified mode
    fd = os.open(str(staging), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, mode)
    try:
        # Wrap FD in a binary file object for the write callback
        f = os.fdopen(fd, 'wb')
        try:
            try:
                write(f)
            except Exception:
                # If write fails, clean up staging file and re-raise
                try:
                    f.close()
                except Exception:
                    pass  # Ignore errors during close
                try:
                    os.unlink(staging)
                except FileNotFoundError:
                    pass  # Already gone
                raise
        finally:
            f.close()
    except Exception:
        # fdopen failed, clean up
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
        raise

    # Close the file descriptor from fdopen (file is now closed)
    # Actually, os.fdopen closes the fd when the context exits, so we don't need to close it here

    # Atomic replace - this should work within same filesystem
    try:
        os.replace(staging, target)
    except Exception:
        # If replace fails, clean up staging file
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass  # Already gone
        raise


def remove_staged_siblings(directory: Path, names: Iterable[str]) -> None:
    """
    Remove staging sibling files (.new files) for given base names.

    Deletes exactly <name>.new for each name in names within directory.
    Does not glob - performs exact name matching.

    Args:
        directory: Directory to search in
        names: Iterable of base file names (without .new suffix)

    Raises:
        ValueError: If any name contains path separators
    """
    for name in names:
        # Check for path separators
        if '/' in name or '\\' in name:
            raise ValueError(f"Name must not contain path separators: {name!r}")

        # Construct the staging file path
        staging_file = directory / f"{name}.new"

        # Try to remove it - ignore if missing (idempotent)
        try:
            staging_file.unlink()
        except FileNotFoundError:
            pass  # Silently ignore missing files