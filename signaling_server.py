import json
import logging
from aiohttp import web, WSMsgType
import os

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SignalingServer:
    def __init__(self):
        self.clients = {}
        self.app = web.Application()
        self.app.router.add_get('/ws', self.websocket_handler)
        self.app.router.add_get('/', self.health_check)

    async def health_check(self, request):
        return web.Response(text="P2P Messenger Signaling Server is running!")

    async def websocket_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        client_id = None
        logger.info("New WebSocket connection")

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type')

                    if msg_type == 'register':
                        client_id = data.get('client_id')
                        self.clients[client_id] = ws
                        await ws.send_text(json.dumps({
                            'type': 'registered',
                            'client_id': client_id,
                            'online_users': list(self.clients.keys())
                        }))
                        logger.info(f"Client registered: {client_id}")

                        # Notify other clients about new user
                        for other_id, other_ws in self.clients.items():
                            if other_id != client_id:
                                try:
                                    await other_ws.send_text(json.dumps({
                                        'type': 'user_online',
                                        'user': client_id
                                    }))
                                except:
                                    pass

                    elif msg_type in ['offer', 'answer', 'ice_candidate', 'call_request', 'call_response']:
                        target_id = data.get('target')
                        if target_id in self.clients:
                            data['sender'] = client_id
                            await self.clients[target_id].send_text(json.dumps(data))
                            logger.info(f"Relayed {msg_type} from {client_id} to {target_id}")
                        else:
                            await ws.send_text(json.dumps({
                                'type': 'error',
                                'message': f'User {target_id} not online'
                            }))

                    elif msg_type == 'message':
                        target_id = data.get('target')
                        if target_id in self.clients:
                            data['sender'] = client_id
                            await self.clients[target_id].send_text(json.dumps(data))
                            logger.info(f"Message from {client_id} to {target_id}")

                except json.JSONDecodeError:
                    logger.error("Invalid JSON received")
                except Exception as e:
                    logger.error(f"Error handling message: {e}")

            elif msg.type == WSMsgType.ERROR:
                logger.error(f'WebSocket error: {ws.exception()}')

        # Clean up on disconnect
        if client_id and client_id in self.clients:
            del self.clients[client_id]
            logger.info(f"Client disconnected: {client_id}")

            # Notify other clients about user going offline
            for other_id, other_ws in self.clients.items():
                try:
                    await other_ws.send_text(json.dumps({
                        'type': 'user_offline',
                        'user': client_id
                    }))
                except:
                    pass

        return ws


async def create_app():
    server = SignalingServer()
    return server.app


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8080))
    server = SignalingServer()

    web.run_app(server.app, host='0.0.0.0', port=port)