from fastapi import APIRouter, WebSocket, WebSocketDisconnect

ws_router = APIRouter()


class ConnectionManager:

    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        """Отправка обновления всем подключенным экранам диспетчеров."""
        for connection in list(self.active_connections):
            try:
                await connection.send_json(message)
            except Exception:
                self.disconnect(connection)


manager = ConnectionManager()


@ws_router.websocket("/ws/dashboard")
async def websocket_dashboard(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            # Ожидание входящих пингов от фронтенда
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)