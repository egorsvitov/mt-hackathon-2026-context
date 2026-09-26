import struct
from datetime import datetime, timezone

from crc import is_crc_valid
from schemas import NphHeader, NplHeader, Telemetry


NPL_HEADER_SIZE = 15
NPH_HEADER_SIZE = 10
NAV_CELL_TYPE = 0
NAV_PAYLOAD_SIZE = 26

HANDSHAKE_SERVICE_ID = 0
HANDSHAKE_MESSAGE_TYPE = 100
REALTIME_SERVICE_ID = 1
REALTIME_MESSAGE_TYPE = 101

NAV_STRUCT = struct.Struct("<IIIBBHHHHHBB")


class ProtocolError(ValueError):
    """Ошибка в структуре NDTP-кадра."""


def parse_npl(frame: bytes) -> NplHeader:
    """Проверяет и разбирает заголовок NPL."""
    if len(frame) < NPL_HEADER_SIZE:
        raise ProtocolError("Кадр короче заголовка NPL")

    if frame[:2] != b"\x7e\x7e":
        raise ProtocolError("Неверная сигнатура NPL")

    data_size = int.from_bytes(frame[2:4], byteorder="little")
    packet_type = frame[8]

    if len(frame) != NPL_HEADER_SIZE + data_size:
        raise ProtocolError("Размер кадра не совпадает с dataSize")

    if packet_type != 0x02:
        raise ProtocolError(f"Неизвестный тип NPL: {packet_type}")

    return NplHeader(
        data_size=data_size,
        crc=int.from_bytes(frame[6:8], byteorder="little"),
        packet_type=packet_type,
        unit_id=int.from_bytes(frame[9:13], byteorder="little"),
        request_id=int.from_bytes(frame[13:15], byteorder="little"),
    )


def parse_nph(frame: bytes) -> NphHeader:
    """Разбирает заголовок NPH."""
    nph_offset = NPL_HEADER_SIZE

    if len(frame) < nph_offset + NPH_HEADER_SIZE:
        raise ProtocolError("Кадр короче заголовков NPL и NPH")

    return NphHeader(
        service_id=int.from_bytes(frame[nph_offset:nph_offset + 2], "little"),
        message_type=int.from_bytes(frame[nph_offset + 2:nph_offset + 4], "little"),
        request_id=int.from_bytes(frame[nph_offset + 6:nph_offset + 10], "little"),
    )


def parse_nav00(
    frame: bytes,
    unit_id: int,
    receive_time: datetime,
) -> Telemetry:
    """Разбирает первую навигационную ячейку G6CellNav00."""
    cell_offset = NPL_HEADER_SIZE + NPH_HEADER_SIZE
    payload_offset = cell_offset + 2
    payload_end = payload_offset + NAV_PAYLOAD_SIZE

    if len(frame) < payload_end:
        raise ProtocolError("В realtime-кадре не хватает данных Nav00")

    cell_type = frame[cell_offset]
    if cell_type != NAV_CELL_TYPE:
        raise ProtocolError(f"Первая ячейка имеет тип {cell_type}, ожидался Nav00")

    (
        timestamp,
        raw_lon,
        raw_lat,
        extra_dop,
        _battery_voltage,
        speed_avg,
        _speed_max,
        course,
        _track,
        altitude,
        _satellites,
        _pdop,
    ) = NAV_STRUCT.unpack(frame[payload_offset:payload_end])

    lon = raw_lon / 10_000_000
    lat = raw_lat / 10_000_000

    if not extra_dop & (1 << 6):
        lon = -lon

    if not extra_dop & (1 << 5):
        lat = -lat

    return Telemetry(
        unit_id=unit_id,
        event_time=datetime.fromtimestamp(timestamp, tz=timezone.utc),
        receive_time=receive_time,
        location_valid=bool(extra_dop & (1 << 7)),
        lon=lon,
        lat=lat,
        alt=altitude,
        speed=speed_avg,
        heading=course,
    )


def parse_frame(frame: bytes, receive_time: datetime) -> Telemetry | None:
    """Проверяет NDTP-кадр и возвращает телеметрию для realtime-пакета."""
    npl = parse_npl(frame)
    nph = parse_nph(frame)

    if not is_crc_valid(frame):
        raise ProtocolError("CRC кадра не совпадает")

    if (
        nph.service_id == HANDSHAKE_SERVICE_ID
        and nph.message_type == HANDSHAKE_MESSAGE_TYPE
    ):
        return None

    if (
        nph.service_id != REALTIME_SERVICE_ID
        or nph.message_type != REALTIME_MESSAGE_TYPE
    ):
        raise ProtocolError(
            f"Неизвестное сообщение: service={nph.service_id}, type={nph.message_type}"
        )

    return parse_nav00(frame, npl.unit_id, receive_time)
