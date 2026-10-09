#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
t3xupdate_safe.py - hardened variant of t3xupdate.py (Michael Niewoehner).

The bytes sent to the station are IDENTICAL to the original script:
JC_identity / JC_version queries, version-string echo to enter the
bootloader, '0x<size><bl version>' start string, then the whole file
(header included) in 2048-byte chunks, each acknowledged with b'ack_jcxx'.

What is different:
  1. Robust reads  - waits until the line is idle instead of read(1)+read_all(),
                     so an ACK split across two USB packets is not misread.
  2. Reconnect     - after each station reset, polls up to --wait seconds for
                     the port to re-appear (instead of a fixed 3 s sleep). If
                     the bootloader enumerates with a different USB serial, it
                     falls back to the single 0403:6001 device present.
  3. Logging       - every TX/RX with timestamps goes to t3x_flash_<time>.log,
                     plus USB enumeration snapshots at each reconnect.
  4. Pre-flight    - prints station vs file product/version and asks for an
                     explicit 'yes' before touching the bootloader (--yes skips).
  5. --port        - optional explicit port override.
  6. Image checks  - JCID magic, size, CRC16 and a Cortex-M vector-table
                     sanity check before anything is sent.

"""

import os
import sys
import time
import logging
import argparse

import serial
from serial.tools import list_ports
import crcmod

PREFIXES = ("JCID_T3", "AIXUN_T320", "JCID_T420D")
FTDI_IDS = (0x0403, 0x6001)
CHUNK = 2048
log = logging.getLogger("t3x")

APP_BASE = 0x08010000      # main firmware start (bootloader below), per aixun_t3a_rev
APP_MAX = 0x2E800          # bootloader size limit documented for T3A; assumed similar on T3B
SRAM_LO, SRAM_HI = 0x20000000, 0x20018000   # covers GD32F303 (48 KB) .. GD32F305/307 (96 KB)


def vector_check(blob):
    """Sanity-check the Cortex-M vector table at payload start. Returns (ok, text)."""
    import struct
    payload = len(blob) - 0x100
    sp, reset = struct.unpack("<II", blob[0x100:0x108])
    ok_size = payload <= APP_MAX
    ok_sp = SRAM_LO < sp <= SRAM_HI
    ok_rst = (reset & 1) == 1 and APP_BASE <= (reset & ~1) < APP_BASE + payload
    text = (f"payload {payload} B (limit {APP_MAX}) {'OK' if ok_size else 'TOO BIG'}; "
            f"SP 0x{sp:08X} {'OK' if ok_sp else 'BAD'}; "
            f"Reset 0x{reset:08X} {'OK' if ok_rst else 'BAD'}")
    return ok_size and ok_sp and ok_rst, text


# ------------------------------------------------------------ logging
def setup_logging(debug):
    log.setLevel(logging.DEBUG)
    fname = time.strftime("t3x_flash_%Y%m%d_%H%M%S.log")
    fh = logging.FileHandler(fname)
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter("%(asctime)s.%(msecs)03d %(levelname)s: %(message)s",
                                      "%H:%M:%S"))
    sh = logging.StreamHandler()
    sh.setLevel(logging.DEBUG if debug else logging.INFO)
    sh.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    log.addHandler(fh)
    log.addHandler(sh)
    return fname


def snapshot_ports(tag):
    for p in list_ports.comports():
        if p.vid is not None:
            log.debug(f"[{tag}] {p.device} {p.vid:04X}:{p.pid:04X} SER={p.serial_number} "
                      f"PROD={p.product}")


# ------------------------------------------------------------ firmware file
def parse_update(path):
    with open(path, "rb") as f:
        blob = f.read()
    if blob[:4] != b"JCID":
        raise SystemExit("ERROR: not an update file (no JCID magic).")
    fw_size = int.from_bytes(blob[0x60:0x64], "big") + 0x100
    if fw_size != len(blob):
        raise SystemExit(f"ERROR: size mismatch, header {fw_size} vs file {len(blob)}.")
    crc16 = crcmod.mkCrcFun(0x18005, rev=True, initCrc=0xFFFF, xorOut=0x0000)
    c_file = int.from_bytes(blob[0x64:0x66], "big")
    c_calc = crc16(blob[0x100:])
    if c_file != c_calc:
        raise SystemExit(f"ERROR: CRC mismatch, header {c_file:04X} vs calc {c_calc:04X}.")
    vt_ok, vt_text = vector_check(blob)
    log.info(f"Vector table: {vt_text}")
    if not vt_ok:
        raise SystemExit("ERROR: image does not look like a main-firmware image for this flash layout.")
    product = blob[0x20:0x30].rstrip(b"\xff").split(b"_")[-1]
    version = blob[0x40:0x44]
    if version == b"vers":
        version = blob[0x47:0x4B]
    log.info(f"Update file: {product.decode()} v{version.decode()} ({len(blob)} bytes, CRC {c_calc:04X} OK)")
    return blob, product, version


# ------------------------------------------------------------ station link
class Station:
    def __init__(self, port=None, wait=20):
        self.forced_port = port
        self.wait = wait
        self.ser = None

    def find_port(self, allow_fallback=False):
        ports = list(list_ports.comports())
        if self.forced_port:
            return self.forced_port if any(p.device == self.forced_port for p in ports) else None
        hits = [p for p in ports if p.serial_number and p.serial_number.startswith(PREFIXES)]
        if len(hits) > 1:
            raise SystemExit("ERROR: multiple T3x attached.")
        if hits:
            return hits[0].device
        if allow_fallback:
            ftdi = [p for p in ports if (p.vid, p.pid) == FTDI_IDS]
            if len(ftdi) == 1:
                log.warning(f"No JCID serial found; using the only 0403:6001 device: "
                            f"{ftdi[0].device} SER={ftdi[0].serial_number}")
                return ftdi[0].device
        return None

    def connect(self, allow_fallback=False, tag="connect"):
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
        deadline = time.monotonic() + self.wait
        while time.monotonic() < deadline:
            dev = self.find_port(allow_fallback)
            if dev:
                try:
                    self.ser = serial.Serial(dev, baudrate=115200, timeout=0.05)
                    time.sleep(0.3)  # let the device settle after enumeration
                    self.ser.reset_input_buffer()
                    snapshot_ports(tag)
                    log.debug(f"Opened {dev}")
                    return
                except serial.SerialException as e:
                    log.debug(f"open {dev} failed: {e}")
            time.sleep(0.25)
        snapshot_ports(tag + "-timeout")
        raise SystemExit(f"ERROR: station did not re-appear within {self.wait} s. "
                         "It is probably sitting in the bootloader: do NOT power it off, "
                         "re-run this script.")

    def transfer(self, data, timeout=3.0, idle=0.1):
        shown = data[:32] + (b"..." if len(data) > 32 else b"")
        log.debug(f"TX ({len(data)}): {shown!r}")
        self.ser.reset_input_buffer()
        self.ser.write(data)
        self.ser.flush()
        t0 = time.monotonic()
        rx = bytearray()
        last = None
        while True:
            chunk = self.ser.read(256)       # raises SerialException if device vanishes
            now = time.monotonic()
            if chunk:
                rx += chunk
                last = now
            elif rx and now - last > idle:
                break
            elif not rx and now - t0 > timeout:
                break
        rx = bytes(rx).rstrip(b"\x00")
        log.debug(f"RX ({len(rx)}) after {(time.monotonic() - t0) * 1000:.0f} ms: {rx!r}")
        return rx

    def identity(self):
        return self.transfer(b"JC_identity")

    def raw_version(self):
        return self.transfer(b"JC_version")


# ------------------------------------------------------------ update flow
def enter_bootloader(st):
    ident = st.identity()
    if ident == b"JC_boot":
        log.info("Station already in bootloader.")
        return True
    if ident != b"JC_User":
        log.error(f"Unexpected identity {ident!r}")
        return False

    log.info("Entering bootloader...")
    try:
        ack = st.transfer(st.raw_version())
        if ack != b"JC_reset":
            log.warning(f"No JC_reset ACK (got {ack!r}); continuing as the original does.")
    except serial.SerialException:
        log.warning("Device dropped off USB (expected: station resetting into bootloader).")

    time.sleep(1.0)
    st.connect(allow_fallback=True, tag="after-bootloader-entry")
    for _ in range(3):
        ident = st.identity()
        if ident == b"JC_boot":
            return True
        time.sleep(0.5)
    log.error(f"Could not confirm bootloader. Identity {ident!r}")
    return False


def do_update(args):
    blob, fw_product, fw_version = parse_update(args.file)

    st = Station(args.port, args.wait)
    st.connect(tag="initial")
    raw = st.raw_version()
    ident = st.identity()
    parts = raw.split(b"_")
    product = parts[2] if len(parts) > 2 else b"?"
    version = raw[-4:]
    log.info(f"Station: identity {ident!r}, version string {raw!r}")

    if ident == b"JC_User" and product != fw_product:
        log.error(f"Product mismatch: file {fw_product!r} vs station {product!r}. Aborting.")
        return False

    print("\n  ---- Pre-flight ------------------------------------")
    print(f"  Station : {product.decode(errors='replace')} v{version.decode(errors='replace')} ({ident.decode(errors='replace')})")
    print(f"  File    : {fw_product.decode()} v{fw_version.decode()}  ({len(blob)} bytes, "
          f"{(len(blob) + CHUNK - 1) // CHUNK} chunks)")
    print("  Do not unplug USB or switch off the station until this script finishes.")
    if not args.yes:
        if input("  Type 'yes' to flash: ").strip().lower() != "yes":
            log.info("Aborted by user. Nothing was written.")
            return False

    if not enter_bootloader(st):
        return False

    bl_raw = st.raw_version()
    log.info(f"Bootloader version string: {bl_raw!r}")
    start = f"0x{len(blob):08x}{bl_raw[8:].decode()}".encode()
    ack = st.transfer(start)
    if ack != b"update_jcxx":
        log.error(f"No update start ACK (got {ack!r}). Nothing written yet; re-run is safe.")
        return False

    log.info("Sending firmware...")
    n_chunks = (len(blob) + CHUNK - 1) // CHUNK
    for i, off in enumerate(range(0, len(blob), CHUNK)):
        data = blob[off:off + CHUNK]
        last = i == n_chunks - 1
        try:
            ack = st.transfer(data, timeout=5.0)
        except serial.SerialException:
            if last:
                log.warning("Device dropped after last chunk (expected: station restarting).")
                break
            log.error(f"Device lost at chunk {i + 1}/{n_chunks}. Station should be in "
                      "bootloader: do NOT power off, re-run this script.")
            return False
        if ack != b"ack_jcxx":
            if last and ack == b"":
                log.warning("No ACK on last chunk; station may already be restarting.")
                break
            log.error(f"Bad ACK at chunk {i + 1}/{n_chunks}: {ack!r}. Do NOT power off; re-run.")
            return False
        print(f"\r  {i + 1}/{n_chunks} chunks ({(off + len(data)) * 100 // len(blob)}%)", end="", flush=True)
    print()

    time.sleep(2.0)
    st.connect(allow_fallback=True, tag="after-flash")
    ident = None
    for _ in range(5):
        ident = st.identity()
        if ident:
            break
        time.sleep(1.0)
    raw = st.raw_version()
    log.info(f"After flash: identity {ident!r}, version {raw!r}")
    if ident == b"JC_User" and raw.endswith(fw_version):
        log.info(f"Update successful. Station now runs {fw_product.decode()} v{fw_version.decode()}.")
        return True
    log.error("Could not confirm the new version. Do NOT power off; send me the log.")
    return False


def main():
    ap = argparse.ArgumentParser(description="Hardened AiXun T3x updater")
    ap.add_argument("file", help="update .bin (JCID header)")
    ap.add_argument("-d", "--debug", action="store_true", help="show TX/RX on screen too")
    ap.add_argument("--port", help="explicit serial port, e.g. /dev/ttyUSB0")
    ap.add_argument("--wait", type=int, default=20, help="seconds to wait for re-enumeration (default 20)")
    ap.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    args = ap.parse_args()

    logname = setup_logging(args.debug)
    log.info(f"Full log: {logname}")
    ok = do_update(args)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
