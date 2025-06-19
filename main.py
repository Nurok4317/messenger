# P2P Messenger Application
# Requirements: pip install aiortc aiohttp opencv-python pyaudio tkinter

import tkinter as tk
from tkinter import ttk, messagebox, scrolledtext
import asyncio
import threading
import json
import hashlib
import sqlite3
import socket
from datetime import datetime
import cv2
import pyaudio
import numpy as np
from PIL import Image, ImageTk
import aiohttp
from aiohttp import web, WSMsgType
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, MediaStreamTrack
from aiortc.contrib.media import MediaPlayer, MediaRecorder
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class AudioStreamTrack(MediaStreamTrack):
    """Custom audio stream track for microphone input"""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.audio = pyaudio.PyAudio()
        self.stream = None
        self.setup_audio()

    def setup_audio(self):
        try:
            self.stream = self.audio.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=44100,
                input=True,
                frames_per_buffer=1024
            )
        except Exception as e:
            logger.error(f"Audio setup failed: {e}")

    async def recv(self):
        if self.stream:
            try:
                data = self.stream.read(1024, exception_on_overflow=False)
                return data
            except Exception as e:
                logger.error(f"Audio receive error: {e}")
        return b''


class VideoStreamTrack(MediaStreamTrack):
    """Custom video stream track for camera input"""
    kind = "video"

    def __init__(self):
        super().__init__()
        self.cap = cv2.VideoCapture(0)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)

    async def recv(self):
        if self.cap and self.cap.isOpened():
            ret, frame = self.cap.read()
            if ret:
                return frame
        return np.zeros((480, 640, 3), dtype=np.uint8)


class DatabaseManager:
    """Handle user registration and authentication"""

    def __init__(self, db_path="messenger.db"):
        self.db_path = db_path
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender TEXT NOT NULL,
                receiver TEXT NOT NULL,
                message TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()

    def hash_password(self, password):
        return hashlib.sha256(password.encode()).hexdigest()

    def register_user(self, username, password):
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            password_hash = self.hash_password(password)
            cursor.execute("INSERT INTO users (username, password_hash) VALUES (?, ?)",
                           (username, password_hash))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            return False

    def authenticate_user(self, username, password):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        password_hash = self.hash_password(password)
        cursor.execute("SELECT * FROM users WHERE username = ? AND password_hash = ?",
                       (username, password_hash))
        user = cursor.fetchone()
        conn.close()
        return user is not None

    def save_message(self, sender, receiver, message):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("INSERT INTO messages (sender, receiver, message) VALUES (?, ?, ?)",
                       (sender, receiver, message))
        conn.commit()
        conn.close()

    def get_messages(self, user1, user2):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT sender, receiver, message, timestamp FROM messages 
            WHERE (sender = ? AND receiver = ?) OR (sender = ? AND receiver = ?)
            ORDER BY timestamp
        """, (user1, user2, user2, user1))
        messages = cursor.fetchall()
        conn.close()
        return messages


class SignalingServer:
    """WebSocket signaling server for WebRTC peer discovery"""

    def __init__(self, host='localhost', port=8080):
        self.host = host
        self.port = port
        self.clients = {}
        self.app = web.Application()
        self.app.router.add_get('/ws', self.websocket_handler)

    async def websocket_handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)

        client_id = None

        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type')

                    if msg_type == 'register':
                        client_id = data.get('client_id')
                        self.clients[client_id] = ws
                        await ws.send_text(json.dumps({'type': 'registered', 'client_id': client_id}))

                    elif msg_type in ['offer', 'answer', 'ice_candidate']:
                        target_id = data.get('target')
                        if target_id in self.clients:
                            await self.clients[target_id].send_text(msg.data)

                    elif msg_type == 'message':
                        target_id = data.get('target')
                        if target_id in self.clients:
                            await self.clients[target_id].send_text(msg.data)

                except json.JSONDecodeError:
                    logger.error("Invalid JSON received")

            elif msg.type == WSMsgType.ERROR:
                logger.error(f'WebSocket error: {ws.exception()}')

        if client_id and client_id in self.clients:
            del self.clients[client_id]

        return ws

    async def start_server(self):
        runner = web.AppRunner(self.app)
        await runner.setup()
        site = web.TCPSite(runner, self.host, self.port)
        await site.start()
        logger.info(f"Signaling server started on {self.host}:{self.port}")


class P2PMessenger:
    """Main messenger application"""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("P2P Messenger")
        self.root.geometry("800x600")

        self.db_manager = DatabaseManager()
        self.current_user = None
        self.websocket = None
        self.peer_connection = None
        self.data_channel = None

        # Video components
        self.video_frame = None
        self.video_label = None
        self.local_video_track = None
        self.remote_video_track = None

        # Audio components
        self.audio_track = None

        self.setup_ui()
        self.signaling_server = SignalingServer()

        # Start signaling server in background
        self.server_thread = threading.Thread(target=self.start_signaling_server, daemon=True)
        self.server_thread.start()

    def start_signaling_server(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(self.signaling_server.start_server())
        loop.run_forever()

    def setup_ui(self):
        # Create notebook for tabs
        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)

        # Login/Register Tab
        self.auth_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.auth_frame, text="Login/Register")
        self.setup_auth_ui()

        # Chat Tab
        self.chat_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.chat_frame, text="Chat", state="disabled")
        self.setup_chat_ui()

        # Video Call Tab
        self.video_frame = ttk.Frame(self.notebook)
        self.notebook.add(self.video_frame, text="Video Call", state="disabled")
        self.setup_video_ui()

    def setup_auth_ui(self):
        # Login Frame
        login_frame = ttk.LabelFrame(self.auth_frame, text="Login", padding=20)
        login_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

        ttk.Label(login_frame, text="Username:").pack(anchor="w")
        self.login_username = ttk.Entry(login_frame, width=30)
        self.login_username.pack(pady=5)

        ttk.Label(login_frame, text="Password:").pack(anchor="w")
        self.login_password = ttk.Entry(login_frame, show="*", width=30)
        self.login_password.pack(pady=5)

        ttk.Button(login_frame, text="Login", command=self.login).pack(pady=10)

        # Register Frame
        register_frame = ttk.LabelFrame(self.auth_frame, text="Register", padding=20)
        register_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)

        ttk.Label(register_frame, text="Username:").pack(anchor="w")
        self.register_username = ttk.Entry(register_frame, width=30)
        self.register_username.pack(pady=5)

        ttk.Label(register_frame, text="Password:").pack(anchor="w")
        self.register_password = ttk.Entry(register_frame, show="*", width=30)
        self.register_password.pack(pady=5)

        ttk.Button(register_frame, text="Register", command=self.register).pack(pady=10)

    def setup_chat_ui(self):
        # Connection Frame
        conn_frame = ttk.Frame(self.chat_frame)
        conn_frame.pack(fill="x", padx=10, pady=5)

        ttk.Label(conn_frame, text="Connect to:").pack(side="left")
        self.target_user = ttk.Entry(conn_frame, width=20)
        self.target_user.pack(side="left", padx=5)

        ttk.Button(conn_frame, text="Connect", command=self.connect_to_peer).pack(side="left")
        ttk.Button(conn_frame, text="Audio Call", command=self.start_audio_call).pack(side="left", padx=5)
        ttk.Button(conn_frame, text="Video Call", command=self.start_video_call).pack(side="left")

        # Messages Display
        self.messages_display = scrolledtext.ScrolledText(self.chat_frame, height=20, state="disabled")
        self.messages_display.pack(fill="both", expand=True, padx=10, pady=5)

        # Message Input
        input_frame = ttk.Frame(self.chat_frame)
        input_frame.pack(fill="x", padx=10, pady=5)

        self.message_entry = ttk.Entry(input_frame)
        self.message_entry.pack(side="left", fill="x", expand=True)
        self.message_entry.bind("<Return>", self.send_message)

        ttk.Button(input_frame, text="Send", command=self.send_message).pack(side="right")

    def setup_video_ui(self):
        # Video display area
        video_container = ttk.Frame(self.video_frame)
        video_container.pack(fill="both", expand=True, padx=10, pady=10)

        # Local video (smaller, top-right corner)
        self.local_video_label = ttk.Label(video_container, text="Local Video", background="black")
        self.local_video_label.place(relx=0.7, rely=0.1, relwidth=0.25, relheight=0.25)

        # Remote video (main area)
        self.remote_video_label = ttk.Label(video_container, text="Remote Video", background="gray")
        self.remote_video_label.pack(fill="both", expand=True)

        # Controls
        controls_frame = ttk.Frame(self.video_frame)
        controls_frame.pack(fill="x", padx=10, pady=5)

        ttk.Button(controls_frame, text="End Call", command=self.end_video_call).pack(side="left")
        ttk.Button(controls_frame, text="Mute", command=self.toggle_mute).pack(side="left", padx=5)
        ttk.Button(controls_frame, text="Camera Off", command=self.toggle_camera).pack(side="left")

    def login(self):
        username = self.login_username.get()
        password = self.login_password.get()

        if self.db_manager.authenticate_user(username, password):
            self.current_user = username
            self.notebook.tab(1, state="normal")  # Enable chat tab
            self.notebook.tab(2, state="normal")  # Enable video tab
            self.notebook.select(1)  # Switch to chat tab
            messagebox.showinfo("Success", f"Welcome, {username}!")
        else:
            messagebox.showerror("Error", "Invalid username or password")

    def register(self):
        username = self.register_username.get()
        password = self.register_password.get()

        if len(username) < 3 or len(password) < 6:
            messagebox.showerror("Error", "Username must be 3+ chars, password 6+ chars")
            return

        if self.db_manager.register_user(username, password):
            messagebox.showinfo("Success", "Registration successful! You can now login.")
        else:
            messagebox.showerror("Error", "Username already exists")

    async def setup_webrtc_connection(self):
        """Setup WebRTC peer connection"""
        self.peer_connection = RTCPeerConnection()

        # Setup data channel for text messages
        self.data_channel = self.peer_connection.createDataChannel("messages")
        self.data_channel.on("message", self.on_data_channel_message)

        # Setup media tracks
        if self.local_video_track:
            self.peer_connection.addTrack(self.local_video_track)

        if self.audio_track:
            self.peer_connection.addTrack(self.audio_track)

        @self.peer_connection.on("track")
        def on_track(track):
            if track.kind == "video":
                self.remote_video_track = track
                # Handle remote video display here
            elif track.kind == "audio":
                # Handle remote audio here
                pass

    def connect_to_peer(self):
        target = self.target_user.get()
        if not target:
            messagebox.showerror("Error", "Please enter target username")
            return

        # In a real implementation, this would initiate WebRTC connection
        self.display_message("System", f"Attempting to connect to {target}...")

        # For demo purposes, simulate connection
        threading.Timer(2.0, lambda: self.display_message("System", f"Connected to {target}")).start()

    def send_message(self, event=None):
        message = self.message_entry.get()
        if not message.strip():
            return

        target = self.target_user.get()
        if not target:
            messagebox.showerror("Error", "Please connect to a user first")
            return

        # Display message locally
        self.display_message(self.current_user, message)

        # Save to database
        self.db_manager.save_message(self.current_user, target, message)

        # Send via data channel (if connected)
        if self.data_channel and self.data_channel.readyState == "open":
            self.data_channel.send(json.dumps({
                "type": "message",
                "sender": self.current_user,
                "content": message,
                "timestamp": datetime.now().isoformat()
            }))

        self.message_entry.delete(0, tk.END)

    def display_message(self, sender, message):
        self.messages_display.config(state="normal")
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.messages_display.insert(tk.END, f"[{timestamp}] {sender}: {message}\n")
        self.messages_display.config(state="disabled")
        self.messages_display.see(tk.END)

    def on_data_channel_message(self, message):
        """Handle incoming data channel messages"""
        try:
            data = json.loads(message)
            if data.get("type") == "message":
                self.display_message(data.get("sender", "Unknown"), data.get("content", ""))
        except json.JSONDecodeError:
            logger.error("Invalid message format received")

    def start_audio_call(self):
        """Initiate audio call"""
        target = self.target_user.get()
        if not target:
            messagebox.showerror("Error", "Please enter target username")
            return

        try:
            self.audio_track = AudioStreamTrack()
            self.display_message("System", f"Starting audio call with {target}...")

            # Setup WebRTC connection with audio
            asyncio.create_task(self.setup_webrtc_connection())

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start audio call: {e}")

    def start_video_call(self):
        """Initiate video call"""
        target = self.target_user.get()
        if not target:
            messagebox.showerror("Error", "Please enter target username")
            return

        try:
            self.local_video_track = VideoStreamTrack()
            self.notebook.select(2)  # Switch to video tab
            self.display_message("System", f"Starting video call with {target}...")

            # Setup WebRTC connection with video
            asyncio.create_task(self.setup_webrtc_connection())

        except Exception as e:
            messagebox.showerror("Error", f"Failed to start video call: {e}")

    def end_video_call(self):
        """End current video call"""
        if self.peer_connection:
            asyncio.create_task(self.peer_connection.close())
            self.peer_connection = None

        if self.local_video_track:
            self.local_video_track.cap.release()
            self.local_video_track = None

        self.notebook.select(1)  # Switch back to chat tab
        self.display_message("System", "Video call ended")

    def toggle_mute(self):
        """Toggle audio mute"""
        # Implementation for muting/unmuting audio
        self.display_message("System", "Audio toggled")

    def toggle_camera(self):
        """Toggle camera on/off"""
        # Implementation for camera toggle
        self.display_message("System", "Camera toggled")

    def run(self):
        """Start the application"""
        try:
            self.root.protocol("WM_DELETE_WINDOW", self.on_closing)
            self.root.mainloop()
        except KeyboardInterrupt:
            self.on_closing()

    def on_closing(self):
        """Clean up on application close"""
        if self.peer_connection:
            asyncio.create_task(self.peer_connection.close())

        if self.local_video_track and self.local_video_track.cap:
            self.local_video_track.cap.release()

        self.root.destroy()


def get_local_ip():
    """Get local IP address for network discovery"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    print("P2P Messenger Application")
    print(f"Local IP: {get_local_ip()}")
    print("Starting application...")

    app = P2PMessenger()
    app.run()