from fastapi import APIRouter, WebSocket, WebSocketDisconnect

ws_router = APIRouter()


class ConnectionManager:
    """Открытые websocket-подключения дашбордов."""
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        """Принимает новое подключение."""
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        """Убирает закрытое подключение."""
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Отправляет сообщение всем открытым дашбордам."""
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


@ws_router.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    """Websocket для дашборда: держим соединение и шлём в него новые прогнозы."""
    await manager.connect(websocket)
    try:
        while True:
                await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
