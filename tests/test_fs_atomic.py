"""Tests for fs_atomic.py atomic staging write utilities."""

import os
import sys
import pytest
from pathlib import Path
from fs_atomic import staged_write, remove_staged_siblings


def test_staged_write_happy_path(tmp_path):
    """A. staged_write happy path."""
    target = tmp_path / "test.txt"
    data = b"hello"

    def write_func(f):
        f.write(data)

    staged_write(target, 0o644, write_func)

    # Target contains the data
    assert target.read_bytes() == data

    # Mode matches (skip on Windows)
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o644

    # No staging file remains
    assert not (tmp_path / "test.txt.new").exists()


def test_staged_write_writer_raises(tmp_path):
    """B. staged_write with writer raising mid-stream."""
    target = tmp_path / "test.txt"
    # Seed target to check it's unchanged
    target.write_bytes(b"original")

    def write_func(f):
        f.write(b"half")
        raise RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        staged_write(target, 0o644, write_func)

    # Target should be unchanged
    assert target.read_bytes() == b"original"

    # No staging file remains
    assert not (tmp_path / "test.txt.new").exists()


def test_staged_write_writer_returns_without_writing(tmp_path):
    """C. staged_write with writer returning without writing."""
    target = tmp_path / "test.txt"

    def write_func(f):
        # Write nothing and return
        pass

    staged_write(target, 0o644, write_func)

    # Target is created and empty
    assert target.read_bytes() == b""

    # Mode matches (skip on Windows)
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o777 == 0o644

    # No staging file remains
    assert not (tmp_path / "test.txt.new").exists()


def test_staged_write_over_existing_target(tmp_path):
    """D. staged_write over an existing target."""
    target = tmp_path / "test.txt"
    # Seed target with old data
    target.write_bytes(b"old")

    def write_func(f):
        f.write(b"new")

    staged_write(target, 0o644, write_func)

    # Target contains new data
    assert target.read_bytes() == b"new"

    # No staging file remains
    assert not (tmp_path / "test.txt.new").exists()


def test_remove_staged_siblings_basic(tmp_path):
    """E. remove_staged_siblings basic test."""
    # Create test files
    (tmp_path / "a.txt").write_bytes(b"content a")
    (tmp_path / "a.txt.new").write_bytes(b"staging a")
    (tmp_path / "b.txt").write_bytes(b"content b")
    (tmp_path / "b.txt.new").write_bytes(b"staging b")
    (tmp_path / "other.txt").write_bytes(b"other")

    # Remove staging siblings
    remove_staged_siblings(tmp_path, ["a.txt", "b.txt"])

    # Staging files should be gone
    assert not (tmp_path / "a.txt.new").exists()
    assert not (tmp_path / "b.txt.new").exists()

    # Original files should remain
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").exists()
    assert (tmp_path / "other.txt").exists()

    # Contents unchanged
    assert (tmp_path / "a.txt").read_bytes() == b"content a"
    assert (tmp_path / "b.txt").read_bytes() == b"content b"

    # Call again (idempotent) - should not error
    remove_staged_siblings(tmp_path, ["a.txt", "b.txt"])


def test_remove_staged_siblings_rejects_path_separators():
    """F. remove_staged_siblings rejects path separators."""
    with pytest.raises(ValueError, match="Name must not contain path separators"):
        remove_staged_siblings(Path("."), ["subdir/file.txt"])

    with pytest.raises(ValueError, match="Name must not contain path separators"):
        remove_staged_siblings(Path("."), ["../escape"])


def test_remove_staged_siblings_does_not_glob(tmp_path):
    """G. remove_staged_siblings does not glob."""
    # Create files
    (tmp_path / "a.txt.new").write_bytes(b"staging a")
    (tmp_path / "c.txt.new").write_bytes(b"staging c")
    (tmp_path / "a.txt").write_bytes(b"content a")
    (tmp_path / "c.txt").write_bytes(b"content c")

    # Remove only a.txt's staging file
    remove_staged_siblings(tmp_path, ["a.txt"])

    # Only a.txt.new should be removed
    assert not (tmp_path / "a.txt.new").exists()
    assert (tmp_path / "c.txt.new").exists()  # Should remain

    # Original files unchanged
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "c.txt").exists()