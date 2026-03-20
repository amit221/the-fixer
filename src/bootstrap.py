"""
Generate .cursor-agent bootstrap script based on detected repo structure.
The bootstrap runs once inside the container to install deps (npm, pip, etc.).
"""

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def detect_stack(repo_path: str) -> str:
    """
    Detect primary stack: node, python, rust, or generic.

    Args:
        repo_path: Path to cloned repo root.

    Returns:
        One of: 'node', 'python', 'rust', 'generic'
    """
    p = Path(repo_path)
    if not p.exists():
        return "generic"

    if (p / "package.json").exists():
        return "node"
    if (
        (p / "pyproject.toml").exists()
        or (p / "setup.py").exists()
        or (p / "requirements.txt").exists()
    ):
        return "python"
    if (p / "Cargo.toml").exists():
        return "rust"

    return "generic"


def _bootstrap_node() -> str:
    return """#!/usr/bin/env bash
set -eu
set -o pipefail 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive

# Prefer lockfile for reproducible installs
if [ -f pnpm-lock.yaml ]; then
  npm install -g pnpm 2>/dev/null || true
  pnpm install --frozen-lockfile 2>/dev/null || pnpm install
elif [ -f yarn.lock ]; then
  npm install -g yarn 2>/dev/null || true
  yarn install --frozen-lockfile 2>/dev/null || yarn install
else
  npm ci 2>/dev/null || npm install
fi
"""


def _bootstrap_python() -> str:
    return """#!/usr/bin/env bash
set -eu
set -o pipefail 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive

if [ -f pyproject.toml ]; then
  pip install -e . 2>/dev/null || pip install -r requirements.txt 2>/dev/null || true
elif [ -f requirements.txt ]; then
  pip install -r requirements.txt 2>/dev/null || true
else
  pip install -e . 2>/dev/null || true
fi
"""


def _bootstrap_rust() -> str:
    return """#!/usr/bin/env bash
set -eu
set -o pipefail 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive

if ! command -v cargo &>/dev/null; then
  curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
  export PATH="$HOME/.cargo/bin:$PATH"
fi

cargo fetch 2>/dev/null || cargo build 2>/dev/null || true
"""


def _bootstrap_generic() -> str:
    return """#!/usr/bin/env bash
set -eu
set -o pipefail 2>/dev/null || true
export DEBIAN_FRONTEND=noninteractive

# Minimal bootstrap: ensure git and basic tools
sudo apt-get update -qq 2>/dev/null || true
sudo apt-get install -y --no-install-recommends git ca-certificates curl 2>/dev/null || true
"""


def generate_bootstrap(repo_path: str) -> str:
    """
    Generate .cursor-agent script content for the given repo.

    Args:
        repo_path: Path to cloned repo root.

    Returns:
        Full bash script content for .cursor-agent.
    """
    stack = detect_stack(repo_path)
    if stack == "node":
        return _bootstrap_node()
    if stack == "python":
        return _bootstrap_python()
    if stack == "rust":
        return _bootstrap_rust()
    return _bootstrap_generic()


def write_bootstrap(repo_path: str, output_path: str | None = None) -> str:
    """
    Write .cursor-agent bootstrap to the repo (or custom path).

    Uses iynx.cursor-agent to match cli-agent-container's *.cursor-agent glob.

    Args:
        repo_path: Path to cloned repo root.
        output_path: Optional. Default: repo_path/iynx.cursor-agent

    Returns:
        Path to written file.
    """
    content = generate_bootstrap(repo_path)
    out = Path(output_path or os.path.join(repo_path, "iynx.cursor-agent"))
    out.parent.mkdir(parents=True, exist_ok=True)
    # Force LF so scripts stay valid inside Linux Docker when the host is Windows
    # (default text mode would emit CRLF and bash would reject e.g. `pipefail\r`).
    out.write_text(content, encoding="utf-8", newline="\n")
    out.chmod(0o755)
    logger.info("Wrote bootstrap to %s (stack: %s)", out, detect_stack(repo_path))
    return str(out)


if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO)
    path = sys.argv[1] if len(sys.argv) > 1 else "."
    write_bootstrap(path)
    print("Bootstrap written for:", path)
