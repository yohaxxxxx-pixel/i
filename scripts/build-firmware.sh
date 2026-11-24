#!/usr/bin/env bash
# Builds iOS-OS (an iOS style userland for the NumWorks n0110, n0115 and n0120) on YOUR
# machine, from NumWorks' official sources.
#
#   1. downloads Epsilon from github.com/numworks/epsilon at the pinned commit
#   2. applies patches/ios-os-epsilon.patch (springboard, control center, apps, and
#      the fix for the Home / On-Off crash)
#   3. compiles the userland for slot B of the n0120, n0115 and n0110
#      (third-party apps allowed); MODELS=n0120 to build only one
#   4. fills installer/ with what the one-click installer needs:
#        installer/firmware/  ios-os.<model>.B.dfu + ios-os.<model>.B.elf (symbols)
#        installer/numworks/  NumWorks' dfu.py, and the EADK runtime to link
#                             .nwa games at their address in slot B
#   5. if games/ holds .nwa games, copies them to installer/games/ and
#      pre-links them (installer/firmware/games.bin + games.json), so that
#      installing them needs no ARM toolchain (Windows PCs)
#
# Nothing from NumWorks is stored in this repository: their code is fetched
# from their own repository and only ever lives on your machine.
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT="$(cat "$ROOT/EPSILON_COMMIT")"
SRC="$ROOT/firmware"
VENV="$ROOT/.venv"
OUT="$ROOT/installer"
JOBS="$(getconf _NPROCESSORS_ONLN 2>/dev/null || echo 4)"
MODELS="${MODELS:-n0120 n0115 n0110}"  # NumWorks models to build for

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# Epsilon needs GNU Make >= 4.3 (macOS ships 3.81: brew install make -> gmake)
MAKE_BIN=make
if command -v gmake >/dev/null; then MAKE_BIN=gmake; fi

for tool in git "$MAKE_BIN" python3; do
  command -v "$tool" >/dev/null || { echo "Il manque « $tool »."; exit 1; }
done
if ! command -v arm-none-eabi-gcc >/dev/null; then
  echo "Le compilateur ARM est absent (macOS : brew install --cask gcc-arm-embedded)."
  exit 1
fi

say "1/5  Sources officielles d'Epsilon ($COMMIT)"
if [ ! -d "$SRC/.git" ]; then
  # Without blobs first: only the files of the pinned commit get downloaded.
  git clone --filter=blob:none --no-checkout https://github.com/numworks/epsilon.git "$SRC"
fi
git -C "$SRC" fetch --quiet origin "$COMMIT" 2>/dev/null || true
git -C "$SRC" checkout --quiet --force "$COMMIT"
git -C "$SRC" clean -fdq -e output -e .venv

say "2/5  Application du patch iOS-OS"
git -C "$SRC" apply --whitespace=nowarn "$ROOT/patches/ios-os-epsilon.patch"

say "3/5  Environnement Python de compilation"
[ -x "$VENV/bin/python3" ] || python3 -m venv "$VENV"
"$VENV/bin/pip" -q install setuptools lz4 pypng pyelftools stringcase parse rich black
ln -sfn "$VENV" "$SRC/.venv"
ln -sfn ../.venv "$SRC/epsilon/.venv"

say "4/5  Compilation de iOS-OS ($MODELS, slot B) : quelques minutes par modèle"
mkdir -p "$OUT/firmware" "$OUT/numworks/eadk" "$OUT/games"
rm -f "$OUT"/firmware/*.dfu "$OUT"/firmware/*.elf
for MODEL in $MODELS; do
  echo "   - $MODEL"
  PATH="$VENV/bin:$PATH" "$MAKE_BIN" -C "$SRC/epsilon" PLATFORM="$MODEL" \
    PYTHON="$VENV/bin/python3" -j"$JOBS" userland.allow3rdparty.B.dfu >"$ROOT/.build.log" 2>&1 \
    || { tail -30 "$ROOT/.build.log"; echo "Échec ($MODEL), voir .build.log"; exit 1; }
  BUILD="$SRC/epsilon/output/release/$MODEL"
  cp "$BUILD/userland.allow3rdparty.B.dfu" "$OUT/firmware/ios-os.$MODEL.B.dfu"
  arm-none-eabi-strip --strip-debug -o "$OUT/firmware/ios-os.$MODEL.B.elf" "$BUILD/userland.allow3rdparty.B.elf"
done

# NumWorks' DFU tool and the pyusb copy it ships with
cp "$SRC/epsilon/tools/device/dfu.py" "$OUT/numworks/dfu.py"
rm -rf "$OUT/numworks/usb"
cp -R "$SRC/epsilon/tools/device/usb" "$OUT/numworks/usb"

# EADK runtime for the games, as one relocatable object per CPU family
# (m7h: n0120, double precision FPU; m7f: n0110 and n0115) + its linker script
EADK="$SRC/epsilon/eadk"
TMP="$(mktemp -d)"
INC="-I$EADK/include -I$SRC/shared -I$SRC/shared/ion/src/device"
rm -f "$OUT/numworks/eadk/eadk.o"
for FAMILY in m7h m7f; do
  FPU=fpv5-d16; [ "$FAMILY" = m7f ] && FPU=fpv5-sp-d16
  CPU="-mcpu=cortex-m7 -mthumb -mfloat-abi=hard -mfpu=$FPU -Os -fno-tree-loop-distribute-patterns -DPLATFORM_DEVICE=1 -ffunction-sections -fdata-sections"
  # shellcheck disable=SC2086
  arm-none-eabi-gcc $CPU $INC -c "$EADK/src/platform.c" -o "$TMP/platform.o"
  # shellcheck disable=SC2086
  arm-none-eabi-gcc $CPU $INC -std=c++20 -fno-exceptions -fno-rtti -c "$EADK/src/svc.cpp" -o "$TMP/svc.o"
  # shellcheck disable=SC2086
  arm-none-eabi-gcc $CPU $INC -std=c++20 -fno-exceptions -fno-rtti -c "$EADK/src/trampoline.cpp" -o "$TMP/trampoline.o"
  arm-none-eabi-ld -r "$TMP/platform.o" "$TMP/svc.o" "$TMP/trampoline.o" -o "$OUT/numworks/eadk/eadk.$FAMILY.o"
done
cp "$EADK/src/linker.ld" "$OUT/numworks/eadk/linker.ld"
rm -rf "$TMP"

say "5/5  Jeux"
rm -f "$OUT"/firmware/games*.bin "$OUT/firmware/games.json"
shopt -s nullglob
GAMES=("$ROOT"/games/*.nwa)
if [ ${#GAMES[@]} -gt 0 ]; then
  cp "${GAMES[@]}" "$OUT/games/"
fi
INSTALLED=("$OUT"/games/*.nwa)
if [ ${#INSTALLED[@]} -gt 0 ]; then
  for MODEL in $MODELS; do
    echo "   - jeux pour la $MODEL"
    "$VENV/bin/python3" "$OUT/link_games.py" "$OUT/firmware/ios-os.$MODEL.B.elf" \
      "$OUT/games" "$OUT/firmware/games.$MODEL.bin"
  done
  "$VENV/bin/python3" - "$OUT" <<'PY'
import hashlib, json, sys
from pathlib import Path
out = Path(sys.argv[1])
games = [{"name": p.name, "sha1": hashlib.sha1(p.read_bytes()).hexdigest()}
         for p in sorted((out / "games").glob("*.nwa"))]
(out / "firmware" / "games.json").write_text(json.dumps({"games": games}, indent=1))
PY
  echo "${#INSTALLED[@]} jeu(x) préparé(s)."
else
  echo "Aucun jeu (mets des .nwa dans games/ pour les inclure)."
fi

say "iOS-OS est prêt dans installer/."
echo "Mac : double-clique sur « installer/Installer iOS-OS.command » (ou : python3 installer/server.py)."
echo "Windows : l'installateur .exe se fait sur un PC Windows avec scripts/build-windows-installer.py,"
echo "          ou dans ta copie privée sur GitHub (Actions > Build iOS-OS)."
