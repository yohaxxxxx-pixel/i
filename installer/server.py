#!/usr/bin/env python3
"""iOS-OS installer: a small local web server to install iOS-OS and its
games on a NumWorks n0110, n0115 or n0120 with one click, from a Mac or a Windows PC.

Only listens on 127.0.0.1. Never writes anything outside of slot B: before
flashing, it checks that slot B is the inactive slot (i.e. that the
calculator is running its official software from slot A).

Usage: server.py [--port 8774] [--no-browser]
       server.py --self-test        (checks the installer, used by the build)

  - macOS: Python 3 and libusb (brew install libusb; dfu-util is used too when
    present), started by "Installer iOS-OS.command".
  - Windows: "Installer iOS-OS.exe", this file built with PyInstaller by
    the GitHub workflow (Python, pyusb and libusb inside, nothing to install).
    The .exe also runs NumWorks' dfu.py (--dfu ...) and link_games.py
    (--link-games ...) when the installer starts it as a separate process.
Adding new games needs the Arm GNU toolchain (arm-none-eabi-gcc); the games
already in games/ when the installer was built are pre-linked and need
nothing.
"""

import argparse
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

OS_NAME = "iOS-OS"
FIRMWARE_NAME = "ios-os"
DEFAULT_PORT = 8775  # PSP-OS 8765, Switch-OS 8766, Windows-OS 8767 ... iOS-OS 8774, iOS-OS 8775

FROZEN = getattr(sys, "frozen", False)  # the Windows .exe (PyInstaller)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
DFU_LAUNCH = HERE / "dfu_launch.py"  # NumWorks' dfu.py, with libusb found
LINK_GAMES = HERE / "link_games.py"
# One firmware per calculator model (built by scripts/build-firmware.sh)
MODELS = ("n0120", "n0115", "n0110")
PREBUILT_LIST = HERE / "firmware" / "games.json"


def firmware_dfu(model):
    return HERE / "firmware" / f"{FIRMWARE_NAME}.{model}.B.dfu"


def firmware_elf(model):
    return HERE / "firmware" / f"{FIRMWARE_NAME}.{model}.B.elf"


def prebuilt_games(model):
    return HERE / "firmware" / f"games.{model}.bin"


def available_models():
    return [m for m in MODELS if firmware_dfu(m).exists() and firmware_elf(m).exists()]
GAMES_DIR = HERE / "games"
PAGE = HERE / "index.html"

PYTHON = sys.executable
WINDOWS = os.name == "nt"
if not FROZEN:
    sys.path.insert(0, str(HERE))  # link_games.py, dfu_launch.py


def find_dfu_util():
    path = shutil.which("dfu-util")
    if path:
        return path
    for candidate in ("/opt/homebrew/bin/dfu-util", "/usr/local/bin/dfu-util"):
        if os.path.exists(candidate):
            return candidate
    return None


DFU_UTIL = find_dfu_util()

USERLAND_B = 0x90410000
SLOT_B_START = 0x90400000
SLOT_B_END = 0x90800000
MAX_UPLOAD = 4 * 1024 * 1024


def apps_area(model="n0120"):
    """(start, end) of the external apps area of the built firmware: it
    starts right after the userland, so it moves when the userland grows."""
    import link_games
    sym = link_games.symbols(firmware_elf(model))
    start = sym["_external_apps_flash_start"]
    end = sym["_external_apps_flash_end"]
    # Never write outside of slot B
    assert SLOT_B_START + 0x10000 < start < end <= SLOT_B_END, (hex(start), hex(end))
    return start, end

# ------------------------------------------------------------------ helpers


def run(cmd, timeout=600):
    kwargs = {}
    if WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    try:
        p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env,
                           timeout=timeout, **kwargs)
    except subprocess.TimeoutExpired:
        return 1, "Délai dépassé : " + " ".join(str(c) for c in cmd[1:3])
    except OSError as e:
        return 1, f"Impossible de lancer {cmd[0]} : {e}"
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def dfu(*args, timeout=600):
    """Runs NumWorks' dfu.py in its own process: the calculator is released
    when it ends (on Windows, one program at a time can open it)."""
    if FROZEN:
        return run([sys.executable, "--dfu", *args], timeout=timeout)
    return run([PYTHON, str(DFU_LAUNCH), *args], timeout=timeout)


def link_games_cmd(*args):
    if FROZEN:
        return [sys.executable, "--link-games", *args]
    return [PYTHON, str(LINK_GAMES), *args]


def dfu_query(flag):
    code, out = dfu(flag, timeout=20)
    lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
    if code != 0 or not lines or "Error" in out:
        return None
    return lines[-1]


STATUS_LOCK = threading.Lock()
LAST_STATUS = {"connected": False}


def calculator_status(wait=False):
    """One look at the calculator at a time: two programs opening it at once
    fail on Windows (the page polls the status every few seconds)."""
    global LAST_STATUS
    if not STATUS_LOCK.acquire(blocking=wait):
        return dict(LAST_STATUS)
    try:
        model = dfu_query("--model")
        if model is None:
            status = {"connected": False}
        else:
            slot = dfu_query("--inactive_slot")
            status = {"connected": True, "model": model, "inactiveSlot": slot}
        LAST_STATUS = status
        return dict(status)
    finally:
        STATUS_LOCK.release()


def elf_section(path, wanted):
    """Contents of a section of an ELF32 little-endian file, or None."""
    try:
        data = Path(path).read_bytes()
        if data[:4] != b"\x7fELF" or data[4] != 1 or data[5] != 1:
            return None
        shoff, = struct.unpack_from("<I", data, 0x20)
        shentsize, shnum, shstrndx = struct.unpack_from("<HHH", data, 0x2E)

        def header(i):
            return struct.unpack_from("<IIIIIIIIII", data, shoff + i * shentsize)

        names = header(shstrndx)
        for i in range(shnum):
            h = header(i)
            start = names[4] + h[0]
            name = data[start:data.index(b"\0", start)].decode("ascii", "replace")
            if name == wanted:
                return data[h[4]:h[4] + h[5]]
    except (OSError, ValueError, struct.error, IndexError):
        return None
    return None


def game_name(path):
    raw = elf_section(path, ".rodata.eadk_app_name")
    if not raw:
        return None
    return raw.split(b"\0")[0].decode("utf-8", "replace") or None


def list_games():
    games = []
    for p in sorted(GAMES_DIR.glob("*.nwa"), key=lambda p: p.name.lower()):
        games.append({"file": p.name, "name": game_name(p) or p.stem,
                      "size": p.stat().st_size})
    return games


def games_fingerprint():
    return sorted((p.name.lower(), hashlib.sha1(p.read_bytes()).hexdigest())
                  for p in GAMES_DIR.glob("*.nwa"))


def prebuilt_matches(model="n0120"):
    """True if games.<model>.bin was built (by the GitHub workflow) for
    exactly the games that are in games/ now."""
    if not prebuilt_games(model).exists() or not PREBUILT_LIST.exists():
        return False
    try:
        listed = json.loads(PREBUILT_LIST.read_text(encoding="utf-8"))
        expected = sorted((g["name"].lower(), g["sha1"]) for g in listed["games"])
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return expected == games_fingerprint()


def have_toolchain():
    try:
        import link_games
        link_games.tool("arm-none-eabi-gcc")
        return True
    except SystemExit:
        return False
    except Exception:
        return False


def safe_game_path(filename):
    name = unquote(filename).replace("\\", "/").split("/")[-1]
    if not name.lower().endswith(".nwa") or name.startswith("."):
        return None
    return GAMES_DIR / name


# ---------------------------------------------------------------- install job


class Job:
    def __init__(self):
        self.lock = threading.Lock()
        self.running = False
        self.steps = []
        self.log = []
        self.result = None  # "ok" | "error" | None

    def snapshot(self):
        with self.lock:
            return {"running": self.running, "steps": list(self.steps),
                    "log": "\n".join(self.log[-200:]), "result": self.result}

    def step(self, label, state="running"):
        with self.lock:
            if self.steps and self.steps[-1]["state"] == "running":
                self.steps[-1]["state"] = "done"
            self.steps.append({"label": label, "state": state})

    def fail(self, message):
        with self.lock:
            if self.steps:
                self.steps[-1]["state"] = "error"
            self.log.append("ERREUR: " + message)
            self.result = "error"
            self.running = False

    def write(self, text):
        with self.lock:
            for line in text.replace("\r", "\n").splitlines():
                # Drop progress-bar noise
                if line.strip() and "%" not in line:
                    self.log.append(line.rstrip())


JOB = Job()


def prepare_games(image, model):
    """Writes the games image to `image`. Returns an error message or None."""
    games = list(GAMES_DIR.glob("*.nwa"))
    if games and prebuilt_matches(model):
        shutil.copyfile(prebuilt_games(model), image)
        JOB.write(f"Jeux déjà préparés ({len(games)}) : pas besoin du compilateur ARM.")
        return None
    if games and not have_toolchain():
        return ("Pour ajouter de nouveaux jeux, installe la chaîne ARM "
                "(Arm GNU Toolchain, arm-none-eabi) ou retire les jeux ajoutés.")
    code, out = run(link_games_cmd(str(firmware_elf(model)), str(GAMES_DIR), image))
    JOB.write("\n".join(l for l in out.splitlines() if "RWX" not in l))
    if code != 0:
        return "Impossible de préparer les jeux (voir le journal)."
    return None


def install():
    try:
        JOB.step("Recherche de la calculatrice")
        status = calculator_status(wait=True)
        if not status["connected"]:
            return JOB.fail("Calculatrice introuvable. Branche-la en USB, "
                            "appuie sur Reset (au dos) et attends le menu officiel.")
        JOB.write(f"Modèle: {status['model']}, slot inactif: {status['inactiveSlot']}")
        model = status["model"]
        if model not in MODELS:
            return JOB.fail(f"Modèle {model} non supporté ({OS_NAME} est fait pour les "
                            "NumWorks n0110, n0115 et n0120).")
        if status["inactiveSlot"] != "B":
            return JOB.fail("La calculatrice tourne sur le slot B. Appuie sur Reset au dos "
                            "pour revenir sur le système officiel (slot A), puis réessaie.")
        if model not in available_models():
            return JOB.fail(f"Firmware {OS_NAME} pour la {model} introuvable dans firmware/ : "
                            "compile-le d'abord (voir le README).")

        JOB.step("Préparation des jeux")
        with tempfile.TemporaryDirectory() as tmp:
            image = os.path.join(tmp, "games.bin")
            error = prepare_games(image, model)
            if error:
                return JOB.fail(error)
            apps_start, apps_end = apps_area(model)
            size = os.path.getsize(image)
            if size > apps_end - apps_start:
                return JOB.fail("Trop de jeux : ils ne tiennent pas dans le slot B.")

            JOB.step("Copie des jeux sur la calculatrice")
            if DFU_UTIL:
                code, out = run([DFU_UTIL, "-a", "0", "-s", hex(apps_start), "-D", image])
            else:
                code, out = dfu("-s", hex(apps_start), "-D", image)
            JOB.write(out)
            if code != 0:
                return JOB.fail("Échec de l'écriture des jeux.")

            JOB.step("Vérification des jeux")
            back = os.path.join(tmp, "readback.bin")
            if DFU_UTIL:
                code, out = run([DFU_UTIL, "-a", "0", "-s", f"{hex(apps_start)}:{size}",
                                 "-U", back])
            else:
                code, out = dfu("--upload", f"{hex(apps_start)}:{size}", back)
            if code != 0 or not os.path.exists(back):
                # Reading back is a bonus check: the writing itself already
                # checks the state of every block.
                JOB.write(out)
                JOB.write("Relecture impossible, vérification sautée "
                          "(l'écriture a contrôlé chaque bloc).")
            elif Path(back).read_bytes() != Path(image).read_bytes():
                return JOB.fail("Les jeux relus ne correspondent pas à ce qui a été écrit.")
            else:
                JOB.write("Jeux vérifiés : identiques.")

        JOB.step(f"Installation de {OS_NAME} (slot B)")
        if dfu_query("--inactive_slot") != "B":
            return JOB.fail("Le slot B n'est plus le slot inactif, installation annulée.")
        code, out = dfu("-s", f"{hex(USERLAND_B)}:leave", "-D", str(firmware_dfu(model)))
        JOB.write(out)
        if code != 0:
            return JOB.fail(f"Échec de l'écriture de {OS_NAME}.")

        JOB.step(f"{OS_NAME} démarre sur la calculatrice", "done")
        with JOB.lock:
            JOB.result = "ok"
            JOB.running = False
    except Exception as e:  # pragma: no cover - surfaced to the page
        JOB.fail(f"{type(e).__name__}: {e}")


def start_install():
    with JOB.lock:
        if JOB.running:
            return False
        JOB.running = True
        JOB.steps = []
        JOB.log = []
        JOB.result = None
    threading.Thread(target=install, daemon=True).start()
    return True


# --------------------------------------------------------------------- http


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send_json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def same_origin(self):
        # Basic protection against other websites driving the installer
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return origin is None or origin == f"http://{host}"

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body = PAGE.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/api/status":
            busy = JOB.snapshot()["running"]
            calc = {"connected": None} if busy else calculator_status()
            try:
                model = calc.get("model") if calc.get("model") in available_models() else "n0120"
                start, end = apps_area(model)
                capacity = end - start
            except Exception:
                capacity = 0x260000
            self.send_json({"calculator": calc, "games": list_games(),
                            "firmware": bool(available_models()),
                            "models": available_models(),
                            "appsCapacity": capacity,
                            "platform": "windows" if WINDOWS else "mac"})
        elif self.path == "/api/job":
            self.send_json(JOB.snapshot())
        else:
            self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        if not self.same_origin():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path == "/api/install":
            ok = start_install()
            return self.send_json({"started": ok}, 200 if ok else 409)
        if self.path == "/api/games":
            target = safe_game_path(self.headers.get("X-Filename", ""))
            length = int(self.headers.get("Content-Length", "0"))
            if target is None:
                return self.send_json({"error": "Seuls les fichiers .nwa sont acceptés."}, 400)
            if length <= 0 or length > MAX_UPLOAD:
                return self.send_json({"error": "Fichier vide ou trop gros."}, 400)
            data = self.rfile.read(length)
            if not data.startswith(b"\x7fELF"):
                return self.send_json({"error": "Ce n'est pas un fichier .nwa valide."}, 400)
            target.write_bytes(data)
            if game_name(target) is None:
                target.unlink()
                return self.send_json({"error": "Fichier .nwa invalide (pas de nom d'app)."}, 400)
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)

    def do_DELETE(self):
        if not self.same_origin():
            return self.send_json({"error": "forbidden"}, 403)
        if self.path.startswith("/api/games/"):
            target = safe_game_path(self.path[len("/api/games/"):])
            if target is None or not target.exists():
                return self.send_json({"error": "Jeu introuvable."}, 404)
            target.unlink()
            return self.send_json({"ok": True})
        self.send_json({"error": "not found"}, 404)


class Server(ThreadingHTTPServer):
    # On Windows, SO_REUSEADDR lets a second program take a port that is
    # already in use: keep it off there, so a busy port is really skipped.
    allow_reuse_address = not WINDOWS
    daemon_threads = True


def open_server(first_port):
    """The first free port from first_port (8765 / 8766 / 8767 are PSP-OS,
    Switch-OS, Mint-OS, iOS-OS and iOS-OS: each installer can run next to the others)."""
    for port in range(first_port, first_port + 20):
        try:
            return Server(("127.0.0.1", port), Handler), port
        except OSError:
            continue
    raise SystemExit("Aucun port libre pour l'installateur.")


def self_test():
    """Checks that everything the installer needs is there and works, without
    a calculator. Run by the GitHub workflow on the Windows .exe."""
    import urllib.request

    import dfu_launch

    problems = []
    for path in [p for m in MODELS for p in (firmware_dfu(m), firmware_elf(m))] + [PAGE]:
        print(("ok  " if path.exists() else "MANQUE  ") + str(path.relative_to(HERE)))
        if not path.exists():
            problems.append(str(path))
    print("libusb :", dfu_launch.libusb_path())
    if WINDOWS and dfu_launch.libusb_path() is None:
        problems.append("libusb (DLL de libusb-package)")
    dfu_module = dfu_launch.load_dfu()
    print("dfu.py :", dfu_module.__name__)
    import usb.core
    try:  # (a build machine may have no USB at all: not an error)
        devices = list(usb.core.find(find_all=True, idVendor=0x0483) or [])
        print("USB ok, appareils NumWorks/STM :", len(devices))
    except Exception as e:
        print("USB indisponible ici :", type(e).__name__, e)
    games = list(GAMES_DIR.glob("*.nwa"))
    for model in available_models():
        start, end = apps_area(model)
        print(f"{model} : zone des jeux {start:#x}..{end:#x}, jeux préparés :",
              prebuilt_matches(model) if games else "-")
        if games and not prebuilt_matches(model):
            problems.append(f"games.{model}.bin ne correspond pas aux jeux")
    code, out = dfu("--list", timeout=60)
    print("dfu.py --list (processus séparé) :", code, out.strip()[-300:])
    if any(word in out for word in ("ModuleNotFoundError", "ImportError", "No module named",
                                    "Impossible de lancer")):
        problems.append("dfu.py --list")
    server, port = open_server(DEFAULT_PORT)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=60) as r:
        status = json.loads(r.read())
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=60) as r:
        page_ok = OS_NAME.encode() in r.read()
    server.shutdown()
    print("Page :", "ok" if page_ok else "ERREUR", "| /api/status :", status)
    if not page_ok or status.get("firmware") is not True:
        problems.append("serveur")
    if problems:
        print("ÉCHEC :", ", ".join(problems))
        return 1
    print("Auto-test OK")
    return 0


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--dfu":
        import dfu_launch
        return dfu_launch.main(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "--link-games":
        import link_games
        return link_games.main(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if args.self_test:
        return self_test()
    GAMES_DIR.mkdir(exist_ok=True)
    server, port = open_server(args.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"Installateur {OS_NAME} : {url}")
    print("Laisse cette fenêtre ouverte pendant l'installation ; ferme-la pour quitter.")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
