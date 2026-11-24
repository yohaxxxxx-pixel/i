#!/usr/bin/env python3
"""Builds the Windows installer: installer/server.py turned into a .exe with
PyInstaller (Python, pyusb and the libusb DLL inside: nothing to install on
the PC), next to the firmware, the games and the page.

Run on Windows by the GitHub workflow, after scripts/build-firmware.sh (on
Linux) has filled installer/firmware, installer/numworks and installer/games:

    python -m pip install pyinstaller pyusb libusb-package
    python scripts/build-windows-installer.py

Output: dist/<OS>-Windows/<OS>/  ("Installer <OS>.exe" + _internal/ +
firmware/ games/ numworks/eadk/ index.html LISEZMOI.txt), checked with the
.exe's own --self-test. Also works on a Mac or Linux (for testing).
"""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"


def main():
    server = (INSTALLER / "server.py").read_text(encoding="utf-8")
    os_name = re.search(r'^OS_NAME = "([^"]+)"', server, re.M).group(1)
    exe_name = f"Installer {os_name}"
    for needed in ("firmware", "numworks/dfu.py", "numworks/eadk", "index.html"):
        if not (INSTALLER / needed).exists():
            raise SystemExit(f"installer/{needed} manque : lance d'abord scripts/build-firmware.sh")

    build = ROOT / "build" / "windows"
    shutil.rmtree(build, ignore_errors=True)
    src = build / "src"
    src.mkdir(parents=True)
    for name in ("server.py", "dfu_launch.py", "link_games.py"):
        shutil.copy2(INSTALLER / name, src / name)
    # NumWorks' dfu.py, used with pip's pyusb (like NumSocial's installer)
    shutil.copy2(INSTALLER / "numworks" / "dfu.py", src / "numworks_dfu.py")

    subprocess.run([
        sys.executable, "-m", "PyInstaller", str(src / "server.py"),
        "--name", exe_name, "--onedir", "--console", "--noconfirm", "--clean",
        "--paths", str(src),
        "--hidden-import", "dfu_launch",
        "--hidden-import", "link_games",
        "--hidden-import", "numworks_dfu",
        "--hidden-import", "usb.backend.libusb1",
        "--collect-all", "libusb_package",
        "--distpath", str(build / "dist"),
        "--workpath", str(build / "work"),
        "--specpath", str(build),
    ], check=True)

    out = ROOT / "dist" / f"{os_name}-Windows" / os_name
    shutil.rmtree(out.parent, ignore_errors=True)
    shutil.copytree(build / "dist" / exe_name, out)
    shutil.copytree(INSTALLER / "firmware", out / "firmware")
    shutil.copytree(INSTALLER / "numworks" / "eadk", out / "numworks" / "eadk")
    (out / "games").mkdir()
    for game in sorted((INSTALLER / "games").glob("*.nwa")):
        shutil.copy2(game, out / "games" / game.name)
    shutil.copy2(INSTALLER / "index.html", out / "index.html")
    readme = ROOT / "docs" / "LISEZMOI-Windows.txt"
    if readme.exists():
        shutil.copy2(readme, out / "LISEZMOI.txt")

    exe = out / (exe_name + (".exe" if os.name == "nt" else ""))
    print(f"\nAuto-test de {exe.name} :", flush=True)
    subprocess.run([str(exe), "--self-test"], check=True)
    print(f"\nPrêt : {out}")


if __name__ == "__main__":
    main()
