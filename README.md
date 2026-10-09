# AiXun T3x safe updater and probe

Tools and notes for updating the firmware of AiXun T3x soldering stations
(T3A / T3B / T3BS / T320 / T420D) **without AiXun's Windows software**, from
any Linux machine.

> **Tested on:** Kali Linux running on a **Raspberry Pi 5**, with a T3B-labelled
> station that reports itself as T3BS, updated from firmware 1.06 to 1.16
> (October 2026). Other Linux distributions and hosts should work the same way,
> but have not been tested.

This repository adds two scripts on top of the original `t3xupdate.py`:

| Script | Purpose |
|---|---|
| `t3x_probe.py` | **Read-only** communication test. It never enters the bootloader. It reads station info, validates an update file and tests the serial link. |
| `t3xupdate_safe.py` | Hardened updater. It sends **byte-for-byte the same protocol** as `t3xupdate.py`, with more robust reading, reconnecting, logging and pre-flight checks. |
| `t3xupdate.py` | Original updater by Michael Niewöhner. Unchanged. |

> **Disclaimer.** Flashing firmware always carries some risk. These tools
> worked on one unit. They are not endorsed by AiXun/JCID. Use them at your
> own risk.

---

## Contents

1. [Why not Windows?](#1-why-not-windows)
2. [T3B vs T3BS: which firmware?](#2-t3b-vs-t3bs-which-firmware)
3. [Setup (Linux)](#3-setup-linux)
4. [Step 1: probe the station](#4-step-1-probe-the-station-read-only)
5. [Step 2: flash](#5-step-2-flash)
6. [Expected outputs](#6-expected-outputs)
7. [Troubleshooting](#7-troubleshooting)
8. [Technical notes](#8-technical-notes)
9. [Credits and license](#9-credits-and-license)

---

## 1. Why not Windows?

The station's USB interface reports itself as an FTDI FT232
(`USB\VID_0403&PID_6001&REV_0400`). It is **not a genuine FTDI chip**. The
station's GD32 MCU emulates the FTDI interface itself; the USB serial-number
prefix `JCID_T3B_` is compiled into the main firmware. See also
[aixun_t3a_rev](https://github.com/c0d3z3r0/aixun_t3a_rev).

What happens on Windows 10/11:

| Attempt | Result |
|---|---|
| Current FTDI CDM driver (e.g. 2.12.36.x) | **Code 10**, "This device cannot start". The driver rejects the non-genuine device. |
| Old CDM 2.08.x driver, force-installed | Blocked by **Memory Integrity** (Core Isolation), "A driver can't load on this device" |
| Same, with Memory Integrity off | **Code 52**: Windows rejects the old driver's signature |
| AiXun's own driver installer | Installs the current FTDI driver, which brings back Code 10 |

**The Linux kernel's `ftdi_sio` driver accepts the device with no extra
configuration.** The quickest route is therefore any Linux machine: a
Raspberry Pi (tested), an Ubuntu live USB, or WSL2 with `usbipd-win`
(untested).

> Firmware 1.08 lists "Fix USB driver compatibility issues". After updating
> to 1.16 it is worth checking whether Windows accepts the device. This has
> not been confirmed.

---

## 2. T3B vs T3BS: which firmware?

AiXun publishes separate `JC_M_T3B_x.xx.bin` and `JC_M_T3BS_x.xx.bin` images
with the **same firmware version numbers and the same changelogs**. **Do not
decide by the sticker, the stand or the menu.** The only reliable way to know
is to ask the station:

```bash
python3 t3x_probe.py
```

Look at the `Product (field2)` line:

- `T3B` → use `JC_M_T3B_x.xx.bin`
- `T3BS` → use `JC_M_T3BS_x.xx.bin`

The unit tested here had a **T3B sticker, the separate plug-in stand of a
T3B, and "T3B" on the front-panel screen**, yet it reported `T3BS`. Its
factory bootloader also identified as `JC_M_T3BS`. So this board was built
as a T3BS.

What the two 1.16 images have in common and where they differ:

| | `JC_M_T3B_1.16.bin` | `JC_M_T3BS_1.16.bin` |
|---|---|---|
| Size | 166,144 B | 166,144 B |
| Version reply | `JC_M_T3B_version1.16` | `JC_M_T3BS_version1.16` |
| Display name / USB serial prefix | `T3B` / `JCID_T3B_` | `T3B` / `JCID_T3B_` (same) |
| Handle-type strings | `C115`, `C210` | `T115` |
| Bytes that differ | ~84%: separately compiled builds, not a rename | |

The screen shows "T3B" with **either** firmware, so it tells you nothing.
By analogy with the T3A/T3AS (where the "S" firmware targets a GD32F305/307
board revision), the "S" variant may be a different board revision. **Do not
cross-flash.** `t3xupdate.py` and `t3xupdate_safe.py` both refuse a product
mismatch.

---

## 3. Setup (Linux)

### 3.1 System packages

```bash
sudo apt update
sudo apt install -y git python3 python3-venv python3-pip
```

### 3.2 Clone the repository

```bash
git clone https://github.com/dzalf/aixun_t3x_safe_updater_and_probe.git
cd aixun_t3x_safe_updater_and_probe
```

### 3.3 Create and enable the virtual environment

Kali and recent Debian-based systems block `pip install` into the system
Python (`externally-managed-environment`), so use a virtual environment:

```bash
# Create it (once)
python3 -m venv venv

# Enable it (every new terminal)
source venv/bin/activate

# Install the dependencies inside it (once)
pip install --upgrade pip
pip install pyserial crcmod
```

When the environment is active, the prompt shows `(venv)`, for example:

```
┌──(venv)─(kali㉿kali-raspberrypi)-[~/aixun_t3x_safe_updater_and_probe]
└─$
```

Useful checks and commands:

```bash
which python3                                   # should point to .../venv/bin/python3
python3 -c "import serial, crcmod; print('OK')" # dependencies present
deactivate                                      # leave the environment when finished
```

Run all the commands below **with the environment enabled**. If you open a
new terminal, `cd` into the repository and run `source venv/bin/activate`
again.

### 3.4 Serial-port permission

```bash
sudo usermod -aG dialout $USER     # then log out and back in
```

Alternatively, run the scripts with `sudo venv/bin/python3 ...`. Plain
`sudo python3` uses the system Python, which won't see the packages
installed in the virtual environment.

### 3.5 Connect the station

Connect the station **directly**: no USB hub, and preferably a USB-A → USB-C
data cable. Then confirm Linux sees it:

```bash
python3 -m serial.tools.list_ports -v
```

Expected:

```
/dev/ttyUSB0
    desc: JCID_T3B
    hwid: USB VID:PID=0403:6001 SER=JCID_T3B_XXXXXXXXXXXX LOCATION=3-1
```

On a Raspberry Pi you will also see `/dev/ttyAMA10`. That is the Pi's own
UART; ignore it.

Optional kernel check:

```bash
dmesg | tail            # "FTDI USB Serial Device converter now attached to ttyUSB0"
```

### 3.6 Get the firmware

Download the `.bin` files from the unofficial archive
<https://aixun-updates.github.io/>. Download **both**:

- the target firmware, e.g. `JC_M_T3BS_1.16.bin`
- the firmware currently on your station, e.g. `JC_M_T3BS_1.06.bin`, as a
  rollback image. There is no known command to read the firmware back from
  the station, so this file is your only backup.

---

## 4. Step 1: probe the station (read-only)

The probe sends **only** `JC_identity` and `JC_version`, the same queries
the updater sends before it does anything. A whitelist in the code prevents
anything else from being sent. In particular, it never echoes the version
string back to the station, which is what triggers the bootloader.

```bash
# Basic probe
python3 t3x_probe.py

# Probe + validate the update file against the station
python3 t3x_probe.py --fw JC_M_T3BS_1.16.bin

# Full pre-flash check (recommended)
python3 t3x_probe.py --fw JC_M_T3BS_1.16.bin --reader-test 50
```

| Option | Description |
|---|---|
| `--fw FILE` | Offline file check: `JCID` magic, declared size, CRC16, product and firmware version, plus a vector-table sanity check. Then it compares the file's product with the station's. |
| `--reader-test N` | Repeats each query N times using the **original updater's** `read(1)+read_all()` method, and checks every reply is complete |
| `--hardware` | Opt-in: also sends `JC_hardware`. It looks like a read-only query but is undocumented. Leave it out if unsure. |
| `--port DEV` | Use a specific port instead of auto-detecting by USB serial number |
| `--repeat N` | Number of query rounds (default 3) |

Exit code `0` means all checks passed; `2` means at least one failed.
**Do not flash unless every line shows `[PASS]`.**

---

## 5. Step 2: flash

```bash
python3 t3xupdate_safe.py -d JC_M_T3BS_1.16.bin
```

The script prints a pre-flight summary and waits until you type `yes`.

| Option | Description |
|---|---|
| `-d`, `--debug` | Show every TX/RX on screen. Everything is always written to the log file regardless. |
| `--port DEV` | Use an explicit serial port |
| `--wait S` | Seconds to wait for the station to re-enumerate after a reset (default 20) |
| `--yes` | Skip the confirmation prompt |

A full timestamped log is written to `t3x_flash_YYYYMMDD_HHMMSS.log`.

**During the flash:** don't touch the cable or the station, and keep the
host on a stable power supply. On a Raspberry Pi 5, use the official 27 W
supply or equivalent.

The original updater still works too:

```bash
python3 t3xupdate.py -d JC_M_T3BS_1.16.bin
```

### What `t3xupdate_safe.py` changes compared with `t3xupdate.py`

| Change | Reason |
|---|---|
| Reads each reply until the line has been quiet for 100 ms | An ACK split across two USB packets can't be misread |
| Polls for up to `--wait` s after each reset instead of sleeping a fixed 3 s | Slow re-enumeration no longer causes "No T3x found" |
| Falls back to the single `0403:6001` device present if the serial number changes | Copes with a bootloader that enumerates under a different serial number |
| JCID magic, size, CRC16 **and a Cortex-M vector-table check** before any serial traffic | Rejects corrupt files or files for the wrong flash layout |
| Pre-flight confirmation + timestamped log | You can review the plan before committing, and a failure can be diagnosed afterwards |
| Every error message says whether it is safe to re-run | Prevents a panicked power-off while the station is in the bootloader |

---

## 6. Expected outputs

These are real outputs from the test unit (T3BS hardware, firmware 1.06 →
1.16, Raspberry Pi 5 running Kali Linux). Serial numbers are redacted.

### 6.1 Probe with file check and reader test

```
$ python3 t3x_probe.py --fw JC_M_T3BS_1.16.bin --reader-test 50

=== USB / host side =============================================
  Port            : /dev/ttyUSB0
  VID:PID         : 0403:6001
  Serial number   : JCID_T3B_XXXXXXXXXXXX
  Manufacturer    : JCID
  Product         : JCID_T3B
  Location        : 3-1
  bcdDevice (chip rev): 0400
  USB spec        : 1.10
  Speed (Mbit/s)  : 12
  bMaxPower       : 100mA
  Configurations  : 1
  Kernel driver   : ftdi_sio
  FTDI latency    : 16 ms

=== Firmware file (offline check, nothing sent) =================
  File            : JC_M_T3BS_1.16.bin
  Size            : 166144 bytes
  Magic           : JCID (update file)
  Declared size   : 166144 bytes -> OK
  CRC16           : file 1EE0, calc 1EE0 -> OK
  Header name     : b'JC_M_T3BS'
  Product         : T3BS
  Version         : 1.16
  Vector table    : payload 165888 B (limit 190464) OK; SP 0x20005508 OK; Reset 0x08010165 OK

=== Serial session (115200 8N1, same as updater) ================
  Unsolicited data in 1 s: 0 bytes

  [1/3] TX JC_identity  -> 7 bytes, first byte after 35.7 ms
      RX: b'JC_User'
  [1/3] TX JC_version   -> 21 bytes, first byte after 46.2 ms
      RX: b'JC_M_T3BS_version1.06'
  ...

=== Reader test: original read(1)+read_all, 50x per query =======
  100/100 replies complete and identical

=== Decoded =====================================================
  Identity        : b'JC_User' -> APPLICATION (normal firmware running)
  Version string  : b'JC_M_T3BS_version1.06'
  Fields ('_')    : ['JC', 'M', 'T3BS', 'version1.06']
  Product (field2): T3BS
  FW version      : 1.06

=== Verdict =====================================================
  [PASS] Station replies to JC_identity
  [PASS] Identity replies consistent
  [PASS] Station replies to JC_version
  [PASS] Version replies consistent
  [PASS] Station in application mode (JC_User)
  [PASS] Updater's read method returns complete replies
  [PASS] Update file valid (magic/size/CRC/vectors)
  [PASS] Update product matches station

  Station v1.06 vs file v1.16

  All checks passed. Communication looks safe to proceed.
```

### 6.2 Successful flash

```
$ python3 t3xupdate_safe.py -d JC_M_T3BS_1.16.bin
INFO: Full log: t3x_flash_20261008_170022.log
INFO: Vector table: payload 165888 B (limit 190464) OK; SP 0x20005508 OK; Reset 0x08010165 OK
INFO: Update file: T3BS v1.16 (166144 bytes, CRC 1EE0 OK)
DEBUG: Opened /dev/ttyUSB0
DEBUG: TX (10): b'JC_version'
DEBUG: RX (21) after 150 ms: b'JC_M_T3BS_version1.06'
DEBUG: TX (11): b'JC_identity'
DEBUG: RX (7) after 200 ms: b'JC_User'
INFO: Station: identity b'JC_User', version string b'JC_M_T3BS_version1.06'

  ---- Pre-flight ------------------------------------
  Station : T3BS v1.06 (JC_User)
  File    : T3BS v1.16  (166144 bytes, 82 chunks)
  Do not unplug USB or switch off the station until this script finishes.
  Type 'yes' to flash: yes
DEBUG: TX (11): b'JC_identity'
DEBUG: RX (7) after 202 ms: b'JC_User'
INFO: Entering bootloader...
DEBUG: TX (10): b'JC_version'
DEBUG: RX (21) after 201 ms: b'JC_M_T3BS_version1.06'
DEBUG: TX (21): b'JC_M_T3BS_version1.06'
WARNING: Device dropped off USB (expected: station resetting into bootloader).
DEBUG: [after-bootloader-entry] /dev/ttyUSB1 0403:6001 SER=JCID_T3B_XXXXXXXXXXXX PROD=JCID_T3B
DEBUG: Opened /dev/ttyUSB1
DEBUG: TX (11): b'JC_identity'
DEBUG: RX (7) after 150 ms: b'JC_boot'
DEBUG: TX (10): b'JC_version'
DEBUG: RX (29) after 150 ms: b'00160000JC_M_T3BS_version1.01'
INFO: Bootloader version string: b'00160000JC_M_T3BS_version1.01'
DEBUG: TX (31): b'0x00028900JC_M_T3BS_version1.01'
DEBUG: RX (11) after 150 ms: b'update_jcxx'
INFO: Sending firmware...
DEBUG: TX (2048): b'JCID\xff\xff\xff...'
DEBUG: RX (8) after 150 ms: b'ack_jcxx'
  1/82 chunks (1%)
  ...
  81/82 chunks (99%)DEBUG: TX (256): b'\xff\xff\xff...'
WARNING: Device dropped after last chunk (expected: station restarting).

DEBUG: Opened /dev/ttyUSB0
DEBUG: TX (11): b'JC_identity'
DEBUG: RX (7) after 150 ms: b'JC_User'
DEBUG: TX (10): b'JC_version'
DEBUG: RX (21) after 150 ms: b'JC_M_T3BS_version1.16'
INFO: After flash: identity b'JC_User', version b'JC_M_T3BS_version1.16'
INFO: Update successful. Station now runs T3BS v1.16.
```

### 6.3 What is normal and what is not

| Message | Meaning |
|---|---|
| `Device dropped off USB (expected...)` after the version echo | **Normal.** The station is resetting into the bootloader. |
| Port changes, e.g. `ttyUSB0` → `ttyUSB1` | **Normal.** The kernel enumerates the device as a new tty. |
| `JC_boot` + a bootloader version string | **Normal.** On the test unit the bootloader reported `00160000JC_M_T3BS_version1.01`. |
| `Device dropped after last chunk` | **Normal.** The station restarts into the new firmware. |
| `RX ... after 150–200 ms` | **Normal.** This includes the 100 ms quiet-line wait. |
| `Product mismatch` | **Stop.** Wrong firmware variant (T3B vs T3BS). |
| `Bad ACK at chunk n/82` / `Device lost at chunk n/82` | See [Troubleshooting](#7-troubleshooting). **Do not power off.** |

---

## 7. Troubleshooting

| Symptom | Action |
|---|---|
| `error: externally-managed-environment` when running `pip` | The virtual environment is not active. Run `source venv/bin/activate` (see 3.3). |
| `ModuleNotFoundError: No module named 'serial'` | Same cause: activate the virtual environment, or install the dependencies inside it |
| `crcmod` fails to build | `sudo apt install -y build-essential python3-dev`, then `pip install crcmod` again |
| `No T3x found` | Check `python3 -m serial.tools.list_ports -v`. Try a USB-A → USB-C data cable without a hub. Use `--port /dev/ttyUSB0`. |
| `Permission denied` on `/dev/ttyUSB*` | `sudo usermod -aG dialout $USER`, then log in again, or run with `sudo venv/bin/python3 ...` |
| A probe check shows `[FAIL]` | Do not flash. Re-run the probe and investigate first. |
| `No update start ACK` | Nothing has been written yet. Re-running is safe. |
| Failure during the chunk transfer | **Do not power off or unplug.** The station stays in the bootloader. Re-run the same command; the script detects `JC_boot` and starts the transfer again. Keep the log file. |
| `station did not re-appear within 20 s` | Re-run, optionally with `--wait 60` or `--port /dev/ttyUSBx` |
| Rollback | Flash your saved older image, e.g. `JC_M_T3BS_1.06.bin`. The protocol does not appear to block downgrades, but this has **not been tested**. |

The bootloader lives in its own flash region and is not touched by the update
(based on the T3A flash layout). This is why an interrupted transfer should be
recoverable by simply re-running. Note that the T3B has flash read protection
(RDP1), so SWD recovery is **not** a practical fallback: the bootloader is the
recovery path.

---

## 8. Technical notes

### Serial protocol (115200 8N1)

| Host sends | Station replies | Notes |
|---|---|---|
| `JC_identity` | `JC_User` (application) / `JC_boot` (bootloader) | Read-only |
| `JC_version` | `JC_M_T3BS_version1.06` | Read-only. The product is field 2 when split on `_`. |
| *the version string echoed back* | `JC_reset`, then a USB drop | **Enters the bootloader** |
| `JC_version` (in the bootloader) | `00160000JC_M_T3BS_version1.01` | The meaning of the 8-digit prefix is unknown. The updater strips it (`[8:]`). |
| `0x%08x<bootloader version string>` (file size + bootloader string) | `update_jcxx` | Starts the transfer |
| 2048-byte chunks of the **whole file, header included** | `ack_jcxx` per chunk | The last chunk is 256 bytes for these images |

Other command strings found in the firmware, all **unverified**, so do not
send them blindly: `JC_hardware` (probably a read query), `JC_reset`,
`syscfg -v`, `JC RW`, `JC DFU`, `JC_image` / `JC_image_end` / `image_start` /
`image_restart` (likely the custom boot-screen upload), `JCXX_open`.

### Update file format

| Offset | Content |
|---|---|
| `0x00` | `JCID` magic |
| `0x20` | Product string, e.g. `JC_M_T3BS` (padded with `0xFF`) |
| `0x40` | Firmware version, e.g. `1.16` (some T3A images use `version1.26`) |
| `0x60` | Payload size, big-endian (file size = value + 0x100) |
| `0x64` | CRC-16/MODBUS of the payload, big-endian |
| `0x100` | Plaintext Cortex-M image linked at `0x08010000` (not encrypted) |

Vector tables of the 1.16 images:
`T3BS`: SP `0x20005508`, Reset `0x08010165`.
`T3B`: SP `0x20005218`, Reset `0x08010145`.

### Hardware / flash layout

These details are documented for the T3A in
[aixun_t3a_rev](https://github.com/c0d3z3r0/aixun_t3a_rev) and are assumed,
not verified, to be similar for the T3B:

- MCU: GD32F303 (STM32F103-class clone); the USB FTDI interface is emulated
- Bootloader at `0x0000`; main firmware at `0x10000`, maximum `0x2E800` bytes;
  boot-screen image, settings, device-ID HMAC and signature in separate regions
- T3B: read protection (RDP1) enabled

### Relevant firmware changelog entries (T3B and T3BS are identical)

| Firmware | Change |
|---|---|
| 1.15 (2023-12-20) | New "neural network" temperature-control algorithm; new curve screen |
| 1.13 (2023-04-08) | Optimised 210 heating control |
| 1.08 (2022-10-25) | Fixed USB driver compatibility issues |
| 1.16 (2024-06-25) | Release 1.16 |

Full list: <https://aixun-updates.github.io/>

---

## 9. Credits and license

- **Michael Niewöhner ([@c0d3z3r0](https://github.com/c0d3z3r0))**: original
  [`aixun_t3x_updater`](https://github.com/c0d3z3r0/aixun_t3x_updater) and
  [`aixun_t3a_rev`](https://github.com/c0d3z3r0/aixun_t3a_rev). The protocol,
  file format and flash layout in this document come from his work.
- [aixun-updates.github.io](https://aixun-updates.github.io/): unofficial
  firmware archive and changelogs.
- Probe and hardened updater: Dr. Daniel Melendrez,
  [Ivory Electronics](https://www.ivory-electronics.com).

All scripts are licensed under **GPL-2.0-or-later**, the same licence as the
original project.

AiXun and JCID are trademarks of their respective owners. This project is not
affiliated with or endorsed by them.
