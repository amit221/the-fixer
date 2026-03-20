"""Tests for stack detection and bootstrap script generation."""

from pathlib import Path

import bootstrap


def test_detect_stack_missing_path(tmp_path: Path) -> None:
    assert bootstrap.detect_stack(str(tmp_path / "nope")) == "generic"


def test_detect_stack_node(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert bootstrap.detect_stack(str(tmp_path)) == "node"


def test_detect_stack_python_pyproject(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert bootstrap.detect_stack(str(tmp_path)) == "python"


def test_detect_stack_python_requirements(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    assert bootstrap.detect_stack(str(tmp_path)) == "python"


def test_detect_stack_rust(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n', encoding="utf-8")
    assert bootstrap.detect_stack(str(tmp_path)) == "rust"


def test_detect_stack_node_wins_over_python(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    assert bootstrap.detect_stack(str(tmp_path)) == "node"


def test_generate_bootstrap_contains_stack_markers(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    s = bootstrap.generate_bootstrap(str(tmp_path))
    assert "npm" in s or "pnpm" in s

    (tmp_path / "package.json").unlink()
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    s2 = bootstrap.generate_bootstrap(str(tmp_path))
    assert "pip" in s2

    (tmp_path / "requirements.txt").unlink()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "x"\n', encoding="utf-8")
    s3 = bootstrap.generate_bootstrap(str(tmp_path))
    assert "cargo" in s3

    (tmp_path / "Cargo.toml").unlink()
    s4 = bootstrap.generate_bootstrap(str(tmp_path))
    assert "apt-get" in s4 or "git" in s4


def test_write_bootstrap_creates_file(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("x\n", encoding="utf-8")
    out = tmp_path / "custom.cursor-agent"
    path = bootstrap.write_bootstrap(str(tmp_path), output_path=str(out))
    assert path == str(out)
    assert out.exists()
    raw = out.read_bytes()
    assert b"\r" not in raw, "bootstrap must use LF-only newlines for Linux Docker mounts"
    assert "pip" in raw.decode("utf-8")
