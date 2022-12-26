# Copyright (c) Kuba Szczodrzyński 2022-07-29.

import logging
from abc import ABC
from io import FileIO
from logging import DEBUG, debug, warning
from typing import BinaryIO, Generator, Optional, Tuple, Union

from bk7231tools.serial import BK7231Serial

from ltchiptool import SocInterface
from ltchiptool.soc.bk72xx.util import RBL, BekenBinary
from ltchiptool.util import CRC16, peek
from ltchiptool.util.intbin import betoint, gen2bytes
from ltchiptool.util.logging import VERBOSE, verbose
from uf2tool import UploadContext


def check_app_code_crc(data: bytes) -> Union[bool, None]:
    # b #0x40
    # ldr pc, [pc, #0x14]
    if data[0:8] == b"\x2F\x07\xB5\x94\x35\xFF\x2A\x9B":
        crc = CRC16.CMS.calc(data[0:32])
        crc_found = betoint(data[32:34])
        if crc == crc_found:
            return True
        warning("File CRC16 invalid. Considering as non-CRC file.")
        return
    return None


class BK72XXFlash(SocInterface, ABC):
    bk: BK7231Serial = None

    def flash_connect(self):
        if self.bk is not None:
            return
        self.print_protocol()
        self.bk = BK7231Serial(
            port=self.port,
            baudrate=self.baud,
            link_timeout=self.link_timeout,
            cmnd_timeout=self.read_timeout,
        )
        loglevel = logging.getLogger().getEffectiveLevel()
        if loglevel <= DEBUG:
            self.bk.info = lambda *args: debug(" ".join(args))
        if loglevel <= VERBOSE:
            self.bk.debug = lambda *args: verbose(" ".join(args))
        self.bk.connect()

    def flash_get_size(self) -> int:
        return 0x200000

    def flash_get_file_type(
        self,
        file: FileIO,
        length: int,
    ) -> Optional[Tuple[str, Optional[int], int, int]]:
        data = peek(file, size=96)
        if not data:
            return None
        bk = BekenBinary()

        # app firmware file - opcodes encrypted for 0x10000
        app_code = check_app_code_crc(data)
        if app_code is None:
            pass
        elif app_code:
            return "Beken CRC App", 0x11000, 0, min(length, 0x121000)
        elif not app_code:
            return "Beken Encrypted App", None, 0, 0

        # raw firmware binary
        if data[0:8] == b"\x0E\x00\x00\xEA\x14\xF0\x9F\xE5":
            return "Raw ARM Binary", None, 0, 0

        # RBL file for OTA - 'download' partition
        try:
            rbl = RBL.deserialize(data)
            if rbl.encryption or rbl.compression:
                return "Beken OTA", None, 0, 0
        except ValueError:
            # no OTA RBL - continue checking
            pass

        # tried all known non-CRC formats - make sure CRC is okay
        try:
            bk.uncrc(data[0 : 34 * 2], check=True)
        except ValueError:
            # invalid CRC - nothing more to do
            return None

        # CRC is okay, but it's not app file - try to find bootloader RBL
        # read RBL+CRC and app opcodes
        data = peek(file, size=34 * 4, seek=0x10F9A)
        if not data:
            return None

        # file with bootloader - possibly a full dump
        try:
            rbl_data = gen2bytes(bk.uncrc(data[0 : 34 * 3], check=True))
            rbl = RBL.deserialize(rbl_data)
            if rbl.encryption or rbl.compression:
                return None
        except ValueError:
            # no bootloader RBL - give up
            return None

        # full dump file - encrypted app opcodes at 0x11000
        app_code = check_app_code_crc(data[34 * 3 : 34 * 4])
        if app_code:
            blocks = length // 1024
            if blocks == 2048:
                name = "Beken Full Dump"
            elif blocks == 1192:
                name = "Beken BL+APP Dump"
            else:
                name = "Beken Incomplete Dump"
            return name, 0x11000, 0x11000, min(length - 0x11000, 0x121000)

        return None

    def flash_read_raw(
        self,
        start: int,
        length: int,
        verify: bool = True,
        use_rom: bool = False,
    ) -> Generator[bytes, None, None]:
        self.flash_connect()
        return self.bk.flash_read(start=start, length=length, crc_check=verify)

    def flash_write_raw(
        self,
        start: int,
        length: int,
        data: BinaryIO,
        verify: bool = True,
    ) -> Generator[int, None, None]:
        self.flash_connect()
        yield from self.bk.program_flash(
            io=data,
            io_size=length,
            start=start,
            crc_check=verify,
        )

    def flash_write_uf2(
        self,
        ctx: UploadContext,
    ) -> Generator[Union[int, str], None, None]:
        # collect continuous blocks of data (before linking, as this takes time)
        parts = ctx.collect(ota_idx=1)

        # yield the total writing length
        yield sum(len(part.getvalue()) for part in parts.values())

        # connect to chip
        self.flash_connect()

        # write blocks to flash
        for offset, data in parts.items():
            length = len(data.getvalue())
            data.seek(0)
            yield f"Writing {length} bytes to 0x{offset:06x}"
            yield from self.bk.program_flash(
                io=data,
                io_size=length,
                start=offset,
                crc_check=True,
                dry_run=False,
                really_erase=True,
            )
        # reboot the chip
        self.bk.reboot_chip()
