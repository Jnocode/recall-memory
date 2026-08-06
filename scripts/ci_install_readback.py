"""Fresh-install read-back for the CI ``package`` job (task 10.2 / 10.4).

Run this with the *fresh venv's* interpreter, from a working directory outside
the checkout::

    cd "$RUNNER_TEMP"
    ci-venv/bin/python <checkout>/scripts/ci_install_readback.py <checkout>

Why it is not just ``import recall_memory_mcp``:

``recall-sqlite`` 0.2.0 is already published on PyPI, and the canonical core in
this repository still carries the same 0.2.0 version while its content has moved
on. A plain ``pip install --find-links <local dist> <mcp wheel>`` therefore
happily satisfies ``recall-sqlite>=0.2.0,<0.3`` from the *index* instead of the
locally built wheel -- this was observed, not hypothesised -- and CI would then
package-test a core that is not the one in the checkout. So the read-back
compares every installed module byte-for-byte against the source tree.
"""

from __future__ import annotations

import hashlib
import pathlib
import sys


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fail(message: str) -> None:
    print(f"FAIL: {message}")
    raise SystemExit(1)


def _check_package(
    module_name: str, source_dir: pathlib.Path, venv_marker: str
) -> pathlib.Path:
    module = __import__(module_name)
    installed_init = pathlib.Path(module.__file__).resolve()
    parts = installed_init.parts

    if "site-packages" not in parts:
        _fail(f"{module_name} did not resolve inside site-packages: {installed_init}")
    if venv_marker not in parts:
        _fail(f"{module_name} did not resolve inside {venv_marker}: {installed_init}")

    installed_dir = installed_init.parent
    source_files = sorted(p for p in source_dir.rglob("*.py") if "__pycache__" not in p.parts)
    if not source_files:
        _fail(f"no source .py files found under {source_dir}")

    mismatched: list[str] = []
    missing: list[str] = []
    for source_file in source_files:
        relative = source_file.relative_to(source_dir)
        installed_file = installed_dir / relative
        if not installed_file.is_file():
            missing.append(str(relative))
            continue
        if _sha256(installed_file) != _sha256(source_file):
            mismatched.append(str(relative))

    if missing:
        _fail(f"{module_name}: installed distribution is missing {missing}")
    if mismatched:
        _fail(
            f"{module_name}: installed modules differ from this checkout "
            f"{mismatched} -- the wheel under test is not built from this source"
        )

    print(f"OK  {module_name}: {len(source_files)} modules byte-identical -> {installed_dir}")
    return installed_dir


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2

    checkout = pathlib.Path(argv[1]).resolve()
    cwd = pathlib.Path.cwd().resolve()
    if cwd == checkout or checkout in cwd.parents:
        _fail(f"cwd {cwd} is inside the checkout {checkout}; run from outside it")

    venv_marker = "ci-venv"
    if venv_marker not in pathlib.Path(sys.executable).resolve().parts:
        _fail(f"interpreter {sys.executable} is not the fresh {venv_marker}")

    _check_package("recall", checkout / "src" / "recall", venv_marker)
    _check_package(
        "recall_memory_mcp",
        checkout / "recall-memory-mcp" / "src" / "recall_memory_mcp",
        venv_marker,
    )

    import recall
    import recall_memory_mcp

    core_version = getattr(recall, "__version__", None)
    if not core_version:
        _fail("installed recall exposes no __version__")
    print(f"OK  recall {core_version}")
    print(f"OK  recall_memory_mcp {recall_memory_mcp.__version__}")
    print(f"OK  read-back performed from {cwd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
