#!/bin/bash
# Double-clic : lance l'installateur iOS-OS et ouvre la page dans le navigateur.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:/Applications/ArmGNUToolchain/bin:$PATH"
if [ ! -e /opt/homebrew/lib/libusb-1.0.dylib ] && [ ! -e /usr/local/lib/libusb-1.0.dylib ]; then
  echo "Il manque libusb. Installe-la avec Homebrew (https://brew.sh) :"
  echo "    brew install libusb dfu-util"
  read -r -p "Appuie sur Entrée pour fermer." _
  exit 1
fi
if ! command -v python3 >/dev/null; then
  echo "Il manque Python 3 (https://www.python.org/downloads/ ou : brew install python)."
  read -r -p "Appuie sur Entrée pour fermer." _
  exit 1
fi
exec python3 server.py "$@"
