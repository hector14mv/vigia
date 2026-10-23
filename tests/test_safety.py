"""Deletion guards. Every test works inside pytest's tmp_path: no real path is ever deleted."""

from pathlib import Path

import pytest

from vigia import safety


def test_remove_deletes_a_child_of_the_root(tmp_path):
    root = tmp_path / "work"
    child = root / "build-1"
    (child / "src").mkdir(parents=True)
    (child / "src" / "a.py").write_text("x")
    assert safety.remove(child, root) is True
    assert not child.exists() and root.exists()
    assert safety.remove(child, root) is False


def test_remove_refuses_the_root_itself(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    with pytest.raises(safety.UnsafePath):
        safety.remove(root, root)
    assert root.exists()


def test_remove_refuses_paths_outside_the_root(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    outside = tmp_path / "precious"
    outside.mkdir()
    for path in (outside, root / ".." / "precious", Path("/"), Path.home(), Path("")):
        with pytest.raises(safety.UnsafePath):
            safety.remove(path, root)
    assert outside.exists()


def test_remove_refuses_a_symlink_that_points_out(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    precious = tmp_path / "precious"
    precious.mkdir()
    (precious / "keep.txt").write_text("keep")
    link = root / "link"
    link.symlink_to(precious)
    with pytest.raises(safety.UnsafePath):
        safety.remove(link, root)
    assert (precious / "keep.txt").exists()


@pytest.mark.parametrize("bad", [Path("/"), Path.home(), Path.home().parent])
def test_dangerous_work_roots_are_refused(bad):
    with pytest.raises(safety.UnsafePath):
        safety.check_root(bad)


def test_inside_requires_strict_containment(tmp_path):
    root = tmp_path / "work"
    root.mkdir()
    assert safety.inside(root / "a" / "b", root) == (root / "a" / "b").resolve()
    with pytest.raises(safety.UnsafePath):
        safety.inside(tmp_path / "workbench", root)  # a sibling sharing the prefix is not inside
