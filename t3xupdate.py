#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later

__title__       = "T3x Updater"
__description__ = "Update tool for AiXun T3x"
__author__      = "Michael Niewöhner"
__email__       = "foss@mniewoehner.de"
__license__     = 'GPL-2.0-or-later'
__copyright__   = 'Copyright (c) 2024 Michael Niewöhner'

import os
import sys
import serial
import time
import logging
import crcmod
from logging import debug, info, warning, error
from argparse import ArgumentParser
from serial.tools import list_ports

class T3XUpdater():
    def __init__(self):
        pass

    def get_port(self):
        ports = [p.device for p in list_ports.comports() if p.serial_number and p.serial_number.startswith("JCID_T3")]
        if len(ports) > 1:
            error("Multiple T3x attached.")
            sys.exit(1)

        elif not ports:
            error("No T3x found.")
            sys.exit(1)

        return ports[0]

    def connect(self):
        self.ser = serial.Serial(self.get_port(), baudrate=115200, timeout=3)

    def transfer(self, data):
        debug(f"TX: {data[:32]}{'...' if len(data) > 32 else ''}")
        self.ser.write(data)

        # first read 1 byte to use timeout mechanism
        rx  = self.ser.read(1)
        rx += self.ser.read_all().rstrip(b'\x00')

        debug(f"RX: {rx}")
        return rx

    def get_identity(self):
        return self.transfer(b'JC_identity')

    def get_raw_version(self):
        return self.transfer(b'JC_version')

    def get_version(self):
        return self.get_raw_version()[-4:]

    def get_product(self):
        return self.get_raw_version().split(b'_')[2]

    def parse_update(self, file):
        self.file = open(file, 'rb')
        if self.file.read(4) == b'JCID':
            debug("Detected update file")
        else:
            error("Wrong firmware file. Update file required.")
            return (None, None,0)

        file_size = os.stat(file).st_size
        self.file.seek(0x60)
        fw_size = int.from_bytes(self.file.read(4), "big") + 0x100
        if file_size != fw_size:
            error("Bad firmware file size: Expected {fw_size} bytes but file is {file_size} bytes.")
            return (None, None, 0)


        crc16fn = crcmod.mkCrcFun(0x18005, rev=True, initCrc=0xFFFF, xorOut=0x0000)
        self.file.seek(0x64)
        csum_file = int.from_bytes(self.file.read(2), "big")
        self.file.seek(0x100)
        csum_calc = crc16fn(self.file.read())
        if csum_calc != csum_file:
            error(f"Checksum mismatch: expected {csum_file:04X}, got {csum_calc:04X}")
            return (None, None, 0)

        self.file.seek(0x20)
        fw_product = self.file.read(16).rstrip(b'\xff').split(b'_')[-1]
        self.file.seek(0x40)
        fw_version = self.file.read(4)
        if fw_version == b'vers':
            self.file.seek(0x47)
            fw_version = self.file.read(4)

        info(f"Update: {fw_product.decode()} v{fw_version.decode()} ({file_size} bytes)")

        return (fw_product, fw_version, file_size)

    def enter_bootloader(self):
        identity = self.get_identity()
        if identity == b'JC_boot':
            return True

        elif identity == b'JC_User':
            info(f"Enter bootloader...")
            try:
                ack = self.transfer(self.get_raw_version())
            except serial.serialutil.SerialException:
                warning("Got serial.serialutil.SerialException: The soldering station probably entered bootloader. Let's try to reconnect...")
            else:
                if not ack == b'JC_reset':
                    warning("Got no reset ACK, let's try anyways...")
            finally:
                time.sleep(3)
                self.connect()

            identity = self.get_identity()
            if self.get_identity() == b'JC_boot':
                return True

        error("Could not enter bootloader. Identity {identity.decode()}")
        return False

    def do_update(self, file):
        fw_product, fw_version, fw_size = self.parse_update(file)
        if not (fw_product and fw_version):
            return False

        self.connect()
        product = self.get_product()
        if not fw_product == product:
            error(f"Update product mismatch! fw={fw_product.decode()} vs. hw={product.decode()}")
            return False

        raw_version = self.get_raw_version()
        identity = self.get_identity()

        if not self.enter_bootloader():
            return False

        bl_version = self.get_version()
        info(f"BL version: v{bl_version.decode()}")

        bl_raw_version = self.get_raw_version()
        update_str = f'0x{fw_size:08x}{bl_raw_version[8:].decode()}'.encode()
        ack = self.transfer(update_str)
        if not ack == b'update_jcxx':
            error("No update start ack. Aborting.")
            return False

        print("Send update...", end='')
        self.file.seek(0)
        for offset in range(0, fw_size, 2048):
            data = self.file.read(2048)
            if not data:
                break

            try:
                ack = self.transfer(data)
                if not ack == b'ack_jcxx':
                    error("Update failed!")
                    return False
            except serial.serialutil.SerialException:
                if offset == fw_size - 256:
                    print(flush=True)
                    warning("Got serial.serialutil.SerialException: The soldering station probably finished the update and restarted. Let's try to reconnect...")
                else:
                    error("Update failed!")
                    return False

            percent = int(offset*100/fw_size)
            print(f"{percent}%...", end='')
            sys.stdout.flush()

        print("100%")
        sys.stdout.flush()

        time.sleep(3)
        self.connect()
        identity = self.get_identity()
        version = self.get_version()
        if not (identity == b'JC_User' and version == fw_version):
            error("Update failed!")
            return False

        info("Update successful!")

        return True


def main():
    argp = ArgumentParser("T3x Updater")
    argp.add_argument('file', help='Update binary')
    argp.add_argument('--debug', '-d', action='store_true', help='Enable debug output')
    args = argp.parse_args()

    loglevel = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(level=loglevel, format='%(levelname)s: %(message)s')

    t3xupdate = T3XUpdater()
    if not t3xupdate.do_update(args.file):
        sys.exit(1)


if __name__ == '__main__':
    main()

