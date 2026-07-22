from __future__ import annotations

import argparse
import os
import platform
import sys
from pathlib import Path

import PyInstaller.__main__


def platform_key() -> str:
    operating_system = {"win32": "win32", "linux": "linux", "darwin": "darwin"}.get(
        sys.platform
    )
    machine = platform.machine().lower()
    architecture = (
        "x64"
        if machine in {"amd64", "x86_64"}
        else "arm64"
        if machine in {"arm64", "aarch64"}
        else None
    )
    if operating_system is None or architecture is None:
        raise SystemExit(f"unsupported sidecar build platform: {sys.platform}/{machine}")
    return f"{operating_system}-{architecture}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the bundled Make It So sidecar")
    parser.add_argument("--output-root", type=Path, default=Path("openclaw-plugin/bin"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    key = platform_key()
    output = (root / args.output_root / key).resolve()
    work = (root / "build" / "pyinstaller" / key).resolve()
    output.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    name = "make-it-so-sidecar"
    PyInstaller.__main__.run(
        [
            "--clean",
            "--noconfirm",
            "--onefile",
            "--name",
            name,
            "--collect-data",
            "make_it_so",
            "--distpath",
            str(output),
            "--workpath",
            str(work / "work"),
            "--specpath",
            str(work),
            str(root / "packaging" / "sidecar_entry.py"),
        ]
    )
    executable = output / (f"{name}.exe" if sys.platform == "win32" else name)
    if not executable.is_file():
        raise SystemExit(f"PyInstaller did not produce {executable}")
    if sys.platform != "win32":
        os.chmod(executable, 0o755)
    print(executable)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
