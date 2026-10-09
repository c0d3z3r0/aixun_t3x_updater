#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""
t3x_probe.py - read-only communication test for AiXun T3x stations.

Derived from t3xupdate.py (Michael Niewoehner). This script NEVER enters the
bootloader and NEVER writes firmware. It only sends the two query strings
that t3xupdate.py itself sends before doing anything:

    JC_identity  -> b'JC_User' (application) or b'JC_boot' (bootloader)
    JC_version   -> raw version string, e.g. product + version

IMPORTANT: in t3xupdate.py, sending the *version string back to the station*
is what triggers the reset into the bootloader (enter_bootloader()). This
script enforces a TX whitelist so that can never happen.

Usage:
    python3 t3x_probe.py                      # auto-detect port, probe
    python3 t3x_probe.py --fw JC_M_T3B_x.bin  # also pre-check the update file
    python3 t3x_probe.py --port /dev/ttyUSB0 --repeat 5
    python3 t3x_probe.py --fw JC_M_T3BS_1.16.bin --reader-test 50 [--hardware]

"""

import os
import sys
import time
import argparse

import serial
from serial.tools import list_ports

try:
    import crcmod
except ImportError:
    crcmod = None

SERIAL_PREFIXES = ("JCID_T3", "AIXUN_T320", "JCID_T420D")
ALLOWED_TX = [b"JC_identity", b"JC_version"]   # nothing else is ever sent
OPTIONAL_TX = b"JC_hardware"                    # only with --hardware

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


# ---------------------------------------------------------------- helpers
def hexdump(data, indent="      "):
    if not data:
        return indent + "(no bytes)"
    lines = []
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hx = " ".join(f"{b:02X}" for b in chunk)
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append(f"{indent}{i:04X}  {hx:<47}  {asc}")
    return "\n".join(lines)


def section(title):
    print(f"\n=== {title} " + "=" * max(0, 60 - len(title)))


def read_sysfs(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


# ---------------------------------------------------------------- USB info
def find_port(explicit=None):
    ports = list(list_ports.comports())
    if explicit:
        for p in ports:
            if p.device == explicit:
                return p
        print(f"ERROR: {explicit} not found.")
        sys.exit(1)
    hits = [p for p in ports if p.serial_number and p.serial_number.startswith(SERIAL_PREFIXES)]
    if not hits:
        print("ERROR: No T3x found (no port with serial number starting "
              f"{SERIAL_PREFIXES}).")
        sys.exit(1)
    if len(hits) > 1:
        print("ERROR: Multiple T3x attached:", [p.device for p in hits])
        sys.exit(1)
    return hits[0]


def usb_info(p):
    section("USB / host side")
    print(f"  Port            : {p.device}")
    print(f"  VID:PID         : {p.vid:04X}:{p.pid:04X}" if p.vid is not None else "  VID:PID         : n/a")
    print(f"  Serial number   : {p.serial_number}")
    print(f"  Manufacturer    : {p.manufacturer}")
    print(f"  Product         : {p.product}")
    print(f"  Location        : {p.location}")

    # Extra descriptor fields from sysfs (Linux only)
    name = os.path.basename(p.device)
    dev_link = f"/sys/class/tty/{name}/device"
    if os.path.exists(dev_link):
        iface = os.path.realpath(dev_link)              # .../3-1/3-1:1.0/ttyUSB0
        usbdev = os.path.dirname(os.path.dirname(iface))  # .../3-1
        fields = [("bcdDevice", "bcdDevice (chip rev)"), ("version", "USB spec"),
                  ("speed", "Speed (Mbit/s)"), ("bMaxPower", "bMaxPower"),
                  ("bNumConfigurations", "Configurations")]
        for attr, label in fields:
            v = read_sysfs(os.path.join(usbdev, attr))
            if v is not None:
                print(f"  {label:<16}: {v}")
        drv = os.path.join(dev_link, "driver")
        if os.path.exists(drv):
            print(f"  Kernel driver   : {os.path.basename(os.path.realpath(drv))}")
        lat = read_sysfs(f"/sys/bus/usb-serial/devices/{name}/latency_timer")
        if lat is not None:
            print(f"  FTDI latency    : {lat} ms")


# ---------------------------------------------------------------- serial I/O
def safe_transfer(ser, data, timeout=3.0, idle=0.15):
    """Send a whitelisted query and read the full reply.

    Reads until the line has been idle for `idle` seconds after the first
    byte (more robust than t3xupdate's read(1)+read_all, which can split a
    reply that arrives in two USB packets)."""
    if data not in ALLOWED_TX:
        raise RuntimeError(f"Refusing to send non-whitelisted data: {data!r}")

    ser.reset_input_buffer()
    t0 = time.monotonic()
    ser.write(data)
    ser.flush()

    rx = bytearray()
    first_at = None
    last_at = None
    while True:
        now = time.monotonic()
        n = ser.in_waiting
        if n:
            rx += ser.read(n)
            last_at = now
            if first_at is None:
                first_at = now
        elif rx and now - last_at > idle:
            break
        elif not rx and now - t0 > timeout:
            break
        time.sleep(0.005)

    latency_ms = (first_at - t0) * 1000 if first_at else None
    return bytes(rx), latency_ms


def original_transfer(ser, data):
    """Exact copy of t3xupdate.transfer() read logic: read(1) + read_all().
    Used to check the updater's simpler reader returns complete replies."""
    if data not in ALLOWED_TX:
        raise RuntimeError(f"Refusing to send non-whitelisted data: {data!r}")
    ser.reset_input_buffer()
    old = ser.timeout
    ser.timeout = 3
    try:
        ser.write(data)
        rx = ser.read(1)
        rx += ser.read_all().rstrip(b"\x00")
    finally:
        ser.timeout = old
    return rx


def passive_listen(ser, seconds=1.0):
    ser.reset_input_buffer()
    end = time.monotonic() + seconds
    rx = bytearray()
    while time.monotonic() < end:
        n = ser.in_waiting
        if n:
            rx += ser.read(n)
        time.sleep(0.01)
    return bytes(rx)


# ---------------------------------------------------------------- decoding
def decode_version(raw):
    """Mirror t3xupdate's parsing: product = split('_')[2], version = last 4 bytes."""
    clean = raw.rstrip(b"\x00")
    parts = clean.split(b"_")
    product = parts[2] if len(parts) > 2 else None
    version = clean[-4:] if len(clean) >= 4 else None
    return clean, parts, product, version


# ---------------------------------------------------------------- firmware file
def check_fw(path):
    """Offline header check, identical to t3xupdate.parse_update(). No device I/O."""
    section("Firmware file (offline check, nothing sent)")
    info = {"ok": False, "product": None, "version": None}
    try:
        with open(path, "rb") as f:
            blob = f.read()
    except OSError as e:
        print(f"  ERROR: {e}")
        return info

    print(f"  File            : {path}")
    print(f"  Size            : {len(blob)} bytes")
    if blob[:4] != b"JCID":
        print("  FAIL: header is not 'JCID' -> not an update file.")
        return info
    print("  Magic           : JCID (update file)")

    fw_size = int.from_bytes(blob[0x60:0x64], "big") + 0x100
    size_ok = fw_size == len(blob)
    print(f"  Declared size   : {fw_size} bytes -> {'OK' if size_ok else 'MISMATCH'}")

    crc_ok = None
    csum_file = int.from_bytes(blob[0x64:0x66], "big")
    if crcmod:
        crc16 = crcmod.mkCrcFun(0x18005, rev=True, initCrc=0xFFFF, xorOut=0x0000)
        csum_calc = crc16(blob[0x100:])
        crc_ok = csum_calc == csum_file
        print(f"  CRC16           : file {csum_file:04X}, calc {csum_calc:04X} -> {'OK' if crc_ok else 'MISMATCH'}")
    else:
        print("  CRC16           : skipped (crcmod not installed)")

    product = blob[0x20:0x30].rstrip(b"\xff").split(b"_")[-1]
    version = blob[0x40:0x44]
    if version == b"vers":
        version = blob[0x47:0x4B]
    print(f"  Header name     : {blob[0x20:0x30].rstrip(bytes([0xff]))!r}")
    print(f"  Product         : {product.decode(errors='replace')}")
    print(f"  Version         : {version.decode(errors='replace')}")

    vt_ok, vt_text = vector_check(blob)
    print(f"  Vector table    : {vt_text}")
    info.update(ok=size_ok and crc_ok is not False and vt_ok, product=product, version=version)
    return info


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Read-only probe for AiXun T3x stations")
    ap.add_argument("--port", help="serial port (default: auto-detect by USB serial number)")
    ap.add_argument("--repeat", type=int, default=3, help="query repetitions (default 3)")
    ap.add_argument("--fw", help="optional update .bin to pre-check against the station")
    ap.add_argument("--hardware", action="store_true",
                    help="also send JC_hardware (believed to be a read-only query; opt-in)")
    ap.add_argument("--reader-test", type=int, default=0, metavar="N",
                    help="repeat each query N times using the ORIGINAL updater read method")
    args = ap.parse_args()

    if args.hardware:
        ALLOWED_TX.append(OPTIONAL_TX)

    p = find_port(args.port)
    usb_info(p)

    fw = check_fw(args.fw) if args.fw else None

    section("Serial session (115200 8N1, same as updater)")
    try:
        ser = serial.Serial(p.device, baudrate=115200, timeout=0.1)
    except serial.SerialException as e:
        print(f"  ERROR opening port: {e}")
        print("  Hint: is your user in the 'dialout' group?  sudo usermod -aG dialout $USER")
        sys.exit(1)

    with ser:
        time.sleep(0.2)
        unsolicited = passive_listen(ser, 1.0)
        print(f"  Unsolicited data in 1 s: {len(unsolicited)} bytes")
        if unsolicited:
            print(hexdump(unsolicited))

        results = {c: [] for c in ALLOWED_TX}
        for i in range(args.repeat):
            for cmd in ALLOWED_TX:
                rx, lat = safe_transfer(ser, cmd)
                results[cmd].append(rx)
                lat_s = f"{lat:.1f} ms" if lat is not None else "TIMEOUT"
                print(f"\n  [{i + 1}/{args.repeat}] TX {cmd.decode():<12} -> {len(rx)} bytes, first byte after {lat_s}")
                print(f"      RX: {rx!r}")
                print(hexdump(rx))
            time.sleep(0.2)

        reader_ok = None
        if args.reader_test:
            section(f"Reader test: original read(1)+read_all, {args.reader_test}x per query")
            ref = {c: results[c][0] for c in results}
            bad = 0
            for i in range(args.reader_test):
                for cmd in ref:
                    rx = original_transfer(ser, cmd)
                    if rx != ref[cmd]:
                        bad += 1
                        print(f"  MISMATCH #{i + 1} {cmd.decode()}: {rx!r}")
                    time.sleep(0.05)
            total = args.reader_test * len(ref)
            print(f"  {total - bad}/{total} replies complete and identical")
            reader_ok = bad == 0

    # ------------------------------------------------------------ summary
    section("Decoded")
    if args.hardware:
        hw = results.get(OPTIONAL_TX, [b""])
        print(f"  Hardware        : {hw[0]!r}"
              + ("  (no reply - command probably not in this firmware)" if not hw[0] else ""))
    ids = results[b"JC_identity"]
    vers = results[b"JC_version"]
    id_consistent = len(set(ids)) == 1 and ids[0] != b""
    ver_consistent = len(set(vers)) == 1 and vers[0] != b""

    identity = ids[0].rstrip(b"\x00") if ids else b""
    mode = {b"JC_User": "APPLICATION (normal firmware running)",
            b"JC_boot": "BOOTLOADER (station is already in update mode)"}.get(identity, "UNKNOWN")
    print(f"  Identity        : {identity!r} -> {mode}")

    raw, parts, product, version = decode_version(vers[0] if vers else b"")
    print(f"  Version string  : {raw!r}")
    print(f"  Fields ('_')    : {[x.decode(errors='replace') for x in parts]}")
    print(f"  Product (field2): {product.decode(errors='replace') if product else 'n/a'}")
    print(f"  FW version      : {version.decode(errors='replace') if version else 'n/a'}")

    section("Verdict")
    checks = [
        ("Station replies to JC_identity", bool(ids and ids[0])),
        ("Identity replies consistent", id_consistent),
        ("Station replies to JC_version", bool(vers and vers[0])),
        ("Version replies consistent", ver_consistent),
        ("Station in application mode (JC_User)", identity == b"JC_User"),
    ]
    if args.reader_test:
        checks.append(("Updater's read method returns complete replies", reader_ok))
    if fw is not None:
        checks.append(("Update file valid (magic/size/CRC/vectors)", fw["ok"]))
        checks.append(("Update product matches station",
                       bool(product and fw["product"] and product == fw["product"])))
    for label, ok in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")

    if fw is not None and fw["version"] and version:
        same = fw["version"] == version
        print(f"\n  Station v{version.decode(errors='replace')} vs file v{fw['version'].decode(errors='replace')}"
              + ("  (same version - flashing would be a re-flash)" if same else ""))

    all_ok = all(ok for _, ok in checks)
    print("\n  " + ("All checks passed. Communication looks safe to proceed."
                     if all_ok else "One or more checks FAILED - do not flash yet."))
    sys.exit(0 if all_ok else 2)


if __name__ == "__main__":
    main()
