NPL_HEADER_SIZE = 15
NPH_HEADER_SIZE = 10
SIGNATURE = b"\x7e\x7e"


def extract_frames(buffer: bytearray):
    """Извлекает целые NDTP-кадры из накопленного TCP-потока."""
    while True:
        if len(buffer) < 4:
            return

        if buffer[:2] != SIGNATURE:
            del buffer[0]
            continue

        if len(buffer) < NPL_HEADER_SIZE:
            return

        data_size = int.from_bytes(buffer[2:4], byteorder="little")

        if data_size < NPH_HEADER_SIZE:
            del buffer[0]
            continue

        frame_size = NPL_HEADER_SIZE + data_size

        if len(buffer) < frame_size:
            return

        frame = bytes(buffer[:frame_size])
        del buffer[:frame_size]
        yield frame
