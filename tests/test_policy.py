import io, os, tarfile, shutil
import pytest
from swesim.verifier import policy

PROTECTED = ["tests/**", "**/conftest.py", "pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini", ".github/**", ".gitattributes", ".gitignore"]

def test_protected_matching_case_and_depth():
    assert policy.is_protected("tests/test_x.py", PROTECTED)
    assert policy.is_protected("pkg/sub/conftest.py", PROTECTED)
    assert policy.is_protected("Tests/Test_X.py", PROTECTED)   # case-folded
    assert not policy.is_protected("pkg/mod.py", PROTECTED)

def _make_tar(tmp_path, members):
    p = tmp_path / "app.tar"
    with tarfile.open(p, "w") as t:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            if kind == "file":
                ti.size = len(data); t.addfile(ti, io.BytesIO(data))
            elif kind == "sym":
                ti.type = tarfile.SYMTYPE; ti.linkname = data.decode(); t.addfile(ti)
            elif kind == "dir":
                ti.type = tarfile.DIRTYPE; t.addfile(ti)
            elif kind == "hard":
                ti.type = tarfile.LNKTYPE; ti.linkname = data.decode(); t.addfile(ti)
    return p

def test_safe_extract_rejects_symlinks_and_escapes(tmp_path):
    tar = _make_tar(tmp_path, [("./a.py", "file", b"x"), ("./evil", "sym", b"/etc/passwd"), ("../escape", "file", b"y"), ("./d", "dir", b"")])
    res = policy.safe_extract(tar, tmp_path / "out")
    assert (tmp_path / "out" / "a.py").read_text() == "x"
    kinds = {name for name, _ in res.rejected}
    assert "./evil" in kinds and "../escape" in kinds

def test_change_set_and_apply_drops_protected(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    for root in (pristine, tree):
        (root / "pkg").mkdir(parents=True); (root / "tests").mkdir()
        (root / "pkg" / "mod.py").write_text("a = 1\n"); (root / "tests" / "test_m.py").write_text("def test(): pass\n")
    (tree / "pkg" / "mod.py").write_text("a = 2\n"); (tree / "pkg" / "new.py").write_text("b\n")
    (tree / "tests" / "test_m.py").write_text("def test(): assert True\n"); (pristine / "pkg" / "gone.py").write_text("z\n")
    cs = policy.change_set(pristine, tree)
    assert set(cs.modified) == {"pkg/mod.py", "tests/test_m.py"} and cs.added == ["pkg/new.py"] and cs.deleted == ["pkg/gone.py"]
    target = tmp_path / "apply"; import shutil; shutil.copytree(pristine, target)
    res = policy.apply_change_set(target, tree, cs, PROTECTED)
    assert (target / "pkg" / "mod.py").read_text() == "a = 2\n" and (target / "pkg" / "new.py").exists() and not (target / "pkg" / "gone.py").exists()
    assert (target / "tests" / "test_m.py").read_text() == "def test(): pass\n" and res.dropped == ["tests/test_m.py"]

# ---- additional coverage beyond the brief ----

def test_default_protected_covers_pytest_config_and_package_metadata():
    P = policy.DEFAULT_PROTECTED
    for path in ("conftest.py", "a/b/conftest.py", "sitecustomize.py", "pkg/usercustomize.py", "x.pth", "a/b.pth",
                 "pytest.ini", ".pytest.ini", "tests/pytest.ini", "sub/tox.ini", "sub/setup.cfg", "sub/pyproject.toml",
                 "pytest.toml", ".pytest.toml", "tests/pytest.toml", "a/b/.pytest.toml",
                 ".github/workflows/ci.yml", ".gitattributes", ".gitignore", "uv.lock", "requirements-dev.txt",
                 "pkg.egg-info/entry_points.txt", "src/pkg.egg-info/SOURCES.txt", "Pkg.Egg-Info/PKG-INFO"):
        assert policy.is_protected(path, P), path
    for path in ("pkg/mod.py", "setup.py", "README.md", "tests/test_x.py", "pkg/eggs/info.txt"):
        assert not policy.is_protected(path, P), path

def test_protected_normalizes_unicode_and_backslashes():
    import unicodedata
    nfd = unicodedata.normalize("NFD", "tests/tést_x.py")
    assert policy.is_protected(nfd, ["tests/**"])
    assert policy.is_protected("tests\\test_x.py", ["tests/**"])
    assert policy.is_protected("./tests/test_x.py", ["tests/**"])

def test_safe_extract_rejects_case_collisions(tmp_path):
    tar = _make_tar(tmp_path, [("A.py", "file", b"x"), ("a.py", "file", b"y")])
    with pytest.raises(policy.InvalidArchive):
        policy.safe_extract(tar, tmp_path / "out")

def test_safe_extract_rejects_hardlinks_and_skips_excluded(tmp_path):
    tar = _make_tar(tmp_path, [("a.py", "file", b"x"), ("h", "hard", b"a.py"), (".git/config", "file", b"g"),
                               ("pkg/__pycache__/m.pyc", "file", b"c"), ("run.sh", "file", b"#!/bin/sh\n")])
    with tarfile.open(tar, "a") as t:
        ti = tarfile.TarInfo("run.sh2"); ti.size = 3; ti.mode = 0o755; t.addfile(ti, io.BytesIO(b"abc"))
    res = policy.safe_extract(tar, tmp_path / "out")
    assert ("h", "non-regular") in res.rejected
    assert not (tmp_path / "out" / ".git").exists() and not (tmp_path / "out" / "pkg").exists()
    assert res.extracted == 3
    assert os.access(tmp_path / "out" / "run.sh2", os.X_OK) and not os.access(tmp_path / "out" / "run.sh", os.X_OK)

def test_safe_extract_rejects_absolute_and_backslash_names(tmp_path):
    tar = _make_tar(tmp_path, [("/abs.py", "file", b"x"), ("a\\b.py", "file", b"y"), ("ok.py", "file", b"z")])
    res = policy.safe_extract(tar, tmp_path / "out")
    assert {n for n, _ in res.rejected} == {"/abs.py", "a\\b.py"} and (tmp_path / "out" / "ok.py").exists()

def test_safe_extract_enforces_size_bound(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "MAX_BYTES", 5)
    tar = _make_tar(tmp_path, [("a.py", "file", b"123456")])
    with pytest.raises(policy.InvalidArchive):
        policy.safe_extract(tar, tmp_path / "out")

def test_apply_handles_directory_to_file_swap_and_protected_deletion(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    (pristine / "pkg" / "util").mkdir(parents=True); (pristine / "pkg" / "util" / "__init__.py").write_text("u\n")
    (pristine / "conftest.py").write_text("c\n"); (pristine / "pkg" / "keep.py").write_text("k\n")
    (tree / "pkg").mkdir(parents=True); (tree / "pkg" / "util.py").write_text("flat\n"); (tree / "pkg" / "keep.py").write_text("k\n")
    cs = policy.change_set(pristine, tree)
    assert cs.deleted == ["conftest.py", "pkg/util/__init__.py"] and cs.added == ["pkg/util.py"]
    target = tmp_path / "apply"; shutil.copytree(pristine, target)
    res = policy.apply_change_set(target, tree, cs, policy.DEFAULT_PROTECTED)
    assert res.dropped == ["conftest.py"] and (target / "conftest.py").exists()
    assert (target / "pkg" / "util.py").read_text() == "flat\n" and not (target / "pkg" / "util").exists()
    assert res.applied == ["pkg/util.py", "pkg/util/__init__.py"]

def test_apply_refuses_writes_through_symlinked_parent(tmp_path):
    # base ships docs/link -> ../tests; the submission replaces the link with a real dir holding test_c.py
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    (pristine / "tests").mkdir(parents=True); (pristine / "tests" / "test_c.py").write_text("def test_c(): assert True\n")
    (pristine / "docs").mkdir(); os.symlink("../tests", pristine / "docs" / "link")
    (tree / "tests").mkdir(parents=True); (tree / "tests" / "test_c.py").write_text("def test_c(): assert True\n")
    (tree / "docs" / "link").mkdir(parents=True); (tree / "docs" / "link" / "test_c.py").write_text("def test_c(): pass\n")
    cs = policy.change_set(pristine, tree)
    assert cs.added == ["docs/link/test_c.py"] and cs.modified == [] and cs.deleted == []
    target = tmp_path / "apply"; shutil.copytree(pristine, target, symlinks=True)
    res = policy.apply_change_set(target, tree, cs, ["tests/**"])
    assert (target / "tests" / "test_c.py").read_text() == "def test_c(): assert True\n"
    assert res.applied == [] and res.dropped == ["docs/link/test_c.py"]
    assert res.refused == [("docs/link/test_c.py", "symlink-parent")]
    assert (target / "docs" / "link").is_symlink()

def test_apply_refuses_deletions_through_symlinked_parent(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    (pristine / "tests").mkdir(parents=True); (pristine / "tests" / "test_c.py").write_text("c\n")
    (pristine / "docs").mkdir(); (pristine / "docs" / "keep.txt").write_text("k\n")
    (tree / "tests").mkdir(parents=True); (tree / "tests" / "test_c.py").write_text("c\n"); (tree / "docs").mkdir()
    cs = policy.change_set(pristine, tree)
    assert cs.deleted == ["docs/keep.txt"]
    # the runtime tree differs from the base: docs became a symlink to tests, and a same-named file lives there
    target = tmp_path / "apply"; (target / "tests").mkdir(parents=True)
    (target / "tests" / "test_c.py").write_text("c\n"); (target / "tests" / "keep.txt").write_text("protected\n")
    os.symlink("tests", target / "docs")
    res = policy.apply_change_set(target, tree, cs, ["tests/**"])
    assert res.applied == [] and res.dropped == ["docs/keep.txt"] and res.refused == [("docs/keep.txt", "symlink-parent")]
    assert (target / "tests" / "keep.txt").read_text() == "protected\n"

def test_path_conflicts_are_invalid_archive_not_oserror(tmp_path):
    tar = _make_tar(tmp_path, [("a", "file", b"x"), ("a/b.py", "file", b"y")])
    with pytest.raises(policy.InvalidArchive, match="conflict"):
        policy.safe_extract(tar, tmp_path / "out")
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    pristine.mkdir(); (pristine / "pkg").write_text("i am a file\n")
    (tree / "pkg").mkdir(parents=True); (tree / "pkg" / "m.py").write_text("m\n"); (tree / "pkg2").write_text("i am a file\n")
    cs = policy.change_set(pristine, tree)
    target = tmp_path / "apply"; shutil.copytree(pristine, target)
    (target / "pkg2").mkdir()   # runtime tree has a directory where the submission adds a file
    with pytest.raises(policy.InvalidArchive, match="conflict"):
        policy.apply_change_set(target, tree, cs, [])

def test_change_set_uses_content_not_mtime(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    for root in (pristine, tree):
        root.mkdir(); (root / "same.py").write_text("s\n")
    os.utime(tree / "same.py", (0, 0))
    cs = policy.change_set(pristine, tree)
    assert cs.added == [] and cs.modified == [] and cs.deleted == []

def test_change_hash_is_deterministic_and_content_sensitive(tmp_path):
    pristine = tmp_path / "p"; tree = tmp_path / "t"
    for root in (pristine, tree):
        root.mkdir(); (root / "a.py").write_text("1\n")
    (tree / "a.py").write_text("2\n"); (tree / "b.py").write_text("new\n")
    cs = policy.change_set(pristine, tree)
    h1 = policy.change_hash(pristine, tree, cs); h2 = policy.change_hash(pristine, tree, cs)
    assert h1 == h2 and len(h1) == 64
    (tree / "b.py").write_text("changed\n")
    assert policy.change_hash(pristine, tree, cs) != h1
