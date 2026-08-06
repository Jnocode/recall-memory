"""Version sync checker and retired tag denylist enforcement (Task 10.1)."""

from __future__ import annotations

import pathlib
try:
    import tomllib
except ImportError:
    import tomli as tomllib

RETIRED_TAGS = {"v0.1.0-legacy", "v0.1.1-deprecated"}


def get_project_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent.parent


def check_version_sync() -> str:
    root = get_project_root()
    pyproject_path = root / "pyproject.toml"
    init_path = root / "src" / "recall" / "__init__.py"

    with open(pyproject_path, "rb") as f:
        pyproject_data = tomllib.load(f)
    pyproject_version = pyproject_data.get("project", {}).get("version")

    init_version = None
    for line in init_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            init_version = line.split("=")[1].strip().strip('"\'')
            break

    if not pyproject_version or not init_version:
        raise ValueError(f"Could not find version string: pyproject={pyproject_version}, init={init_version}")

    if pyproject_version != init_version:
        raise ValueError(f"Version mismatch: pyproject.toml ({pyproject_version}) != __init__.py ({init_version})")

    return pyproject_version


def check_retired_tags(tag: str) -> None:
    if tag in RETIRED_TAGS:
        raise ValueError(f"Tag '{tag}' is in the retired tag denylist and cannot be used.")


if __name__ == "__main__":
    v = check_version_sync()
    print(f"Version sync OK: {v}")
