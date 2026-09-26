import socket
from datetime import datetime, timezone
from threading import Thread
from typing import Callable

from framing import extract_frames


FrameHandler = Callable[[bytes, datetime], None]


def receive_connection(
    connection: socket.socket,
    address,
    on_frame: FrameHandler,
) -> None:
    """Принимает TCP-поток одного устройства."""
    buffer = bytearray()

    with connection:
        while True:
            try:
                chunk = connection.recv(4096)
            except OSError as error:
                print(f"Ошибка соединения {address}: {error}")
                return

            if not chunk:
                print(f"Устройство отключилось: {address}")
                return

            buffer.extend(chunk)

            for frame in extract_frames(buffer):
                receive_time = datetime.now(timezone.utc)
                on_frame(frame, receive_time)


def run_server(host: str, port: int, on_frame: FrameHandler) -> None:
    """Запускает TCP-сервер и отдельный поток для каждого устройства."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((host, port))
        server.listen()

        print(f"Слушаю TCP на {host}:{port}")

        while True:
            connection, address = server.accept()
            print(f"Подключилось устройство: {address}")

            thread = Thread(
                target=receive_connection,
                args=(connection, address, on_frame),
                daemon=True,
            )
            thread.start()
