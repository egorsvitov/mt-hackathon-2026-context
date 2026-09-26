NPL_HEADER_SIZE = 15


def crc16_modbus(data: bytes) -> int:
    """Вычисляет CRC-16/Modbus для переданных байтов."""
    crc = 0xFFFF

    for byte in data:
        crc ^= byte

        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1

    return crc


def is_crc_valid(frame: bytes) -> bool:
    """Сравнивает CRC из NPL с CRC, вычисленным по NPH и телу."""
    stored_crc = int.from_bytes(frame[6:8], byteorder="little")
    calculated_crc = crc16_modbus(frame[NPL_HEADER_SIZE:])

    swapped_crc = (
        ((calculated_crc & 0x00FF) << 8)
        | ((calculated_crc & 0xFF00) >> 8)
    )

    return stored_crc == swapped_crc
