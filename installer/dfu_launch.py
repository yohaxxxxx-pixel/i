#!/usr/bin/env python3
"""Runs NumWorks' dfu.py with a libusb that works on macOS and on Windows,
with the Windows fixes of NumSocial's installer.

  - macOS: Homebrew's libusb (brew install libusb), in /opt/homebrew/lib or
    /usr/local/lib; dfu.py's own pyusb copy (numworks/usb).
  - Windows: the libusb DLL of the "libusb-package" Python package (bundled
    in the .exe). The calculator declares itself as a WinUSB device, so
    Windows needs no extra driver. WinUSB refuses SET_CONFIGURATION, which
    dfu.py always sends: it is skipped when the device already has one.
  - In the Windows .exe (PyInstaller), dfu.py is bundled as "numworks_dfu"
    with pip's pyusb, like in NumSocial.

Each call runs in its own process (server.py starts one per command), so the
calculator is released as soon as the command ends: Windows lets only one
program at a time open a USB device.

Usage: dfu_launch.py <dfu.py arguments>
       dfu_launch.py --upload 0xADDRESS:SIZE out.bin   (reads memory back)
"""

import os
import sys
from pathlib import Path

FROZEN = getattr(sys, "frozen", False)
HERE = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent
NUMWORKS = HERE / "numworks"
WINDOWS = os.name == "nt"
TRANSFER_SIZE = 2048  # DFU transfer size of the NumWorks calculators


def libusb_path():
    """Full path of the libusb-1.0 library to use, or None (default search)."""
    if WINDOWS:
        try:
            import libusb_package

            path = libusb_package.get_library_path()
            return str(path) if path else None
        except Exception:
            return None
    for directory in ("/opt/homebrew/lib", "/usr/local/lib"):
        path = os.path.join(directory, "libusb-1.0.dylib")
        if os.path.exists(path):
            return path
    return None


def add_library_dir(directory):
    os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
    if WINDOWS and hasattr(os, "add_dll_directory"):
        try:
            os.add_dll_directory(directory)
        except OSError:
            pass


def patch_usb():
    """Makes every USB lookup (including dfu.py's) use our libusb, and skips
    SET_CONFIGURATION when the device already has a configuration."""
    import usb.backend.libusb1
    import usb.core

    path = libusb_path()
    if path:
        add_library_dir(os.path.dirname(path))
    backend = None
    if path:
        backend = usb.backend.libusb1.get_backend(find_library=lambda _name: path)
    if backend is None:
        backend = usb.backend.libusb1.get_backend()

    original_find = usb.core.find

    def find(*args, **kwargs):
        if backend is not None:
            kwargs.setdefault("backend", backend)
        return original_find(*args, **kwargs)

    usb.core.find = find

    original_set_configuration = usb.core.Device.set_configuration

    def set_configuration(self, *args, **kwargs):
        try:
            if not args and not kwargs and self.get_active_configuration() is not None:
                return
        except usb.core.USBError:
            pass
        try:
            original_set_configuration(self, *args, **kwargs)
        except (usb.core.USBError, NotImplementedError):
            if not WINDOWS:
                raise

    usb.core.Device.set_configuration = set_configuration
    return backend


def load_dfu():
    """Imports NumWorks' dfu.py, with the USB fixes applied first."""
    if FROZEN:
        patch_usb()
        import numworks_dfu as dfu
    else:
        sys.path.insert(0, str(NUMWORKS))  # dfu.py and its pyusb copy
        patch_usb()
        import dfu
    return dfu


def upload(dfu, address, size, out):
    """Reads `size` bytes of the calculator's memory from `address` (DfuSe
    upload, like dfu-util -U) into the file `out`."""
    import usb.util

    dfu.init()
    device = dfu.__dict__["__dev"]
    try:
        dfu.set_address(address)
        dfu.abort_request()
        data = bytearray()
        block = 2  # DfuSe: block n reads at address + (n - 2) * transfer size
        while len(data) < size:
            wanted = min(TRANSFER_SIZE, size - len(data))
            chunk = bytes(device.ctrl_transfer(0xA1, 2, block, 0, wanted, 4000))
            if len(chunk) != wanted:
                raise IOError("lecture incomplète à 0x%08x" % (address + len(data)))
            data += chunk
            block += 1
        dfu.abort_request()
    finally:
        try:
            usb.util.dispose_resources(device)
        except Exception:
            pass
    Path(out).write_bytes(bytes(data))
    print("Lu : %d octets." % len(data))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    dfu = load_dfu()
    if argv and argv[0] == "--upload":
        if len(argv) != 3 or ":" not in argv[1]:
            raise SystemExit("Usage : dfu_launch.py --upload 0xADRESSE:TAILLE sortie.bin")
        address, size = argv[1].split(":")
        upload(dfu, int(address, 16), int(size, 0), argv[2])
        return
    sys.argv = ["dfu.py"] + argv
    dfu.main()


if __name__ == "__main__":
    main()
