import asyncio
import json
import logging
from typing import List, Optional
from enum import Enum
import uuid
import zlib
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
from wyzecam.api import get_camera_stream
from wyzecam.api_models import WyzeCamera, WyzeCredential
from wyzecam.kinesis.wpk_stream_info_model import IceServer

logger = logging.getLogger(__name__)


class WebRtcState(Enum):
    """Enum representing WebRTC connection states."""

    CONNECTING = "CONNECTING"
    CONNECTED = "CONNECTED"
    DISCONNECTING = "DISCONNECTING"
    DISCONNECTED = "DISCONNECTED"

    @classmethod
    def values(cls):
        """Get list of all states."""
        return list(cls)

    @classmethod
    def valueOf(cls, name: str):
        """Get state by name."""
        return cls[name]


class WebRtcClient:
    def __init__(
        self, stream_listener=None, status_listener=None, webrtc_statistics=None
    ):
        """Initialize WebRTC client."""
        self.stream_listener = stream_listener
        self.status_listener = status_listener
        self.webrtc_statistics = webrtc_statistics
        self.state = WebRtcState.DISCONNECTED
        self.webrtc_context.ice_servers = []
        self.ice_servers = []
        self.ice_candidates_before_sdp_answer: List = []

        # Instance variables
        self.session_id: Optional[str] = None
        self.session_id_crc32: Optional[str] = None
        self.context = None
        self.mac = None
        self.user_id = None
        self.access_token = None
        self.model = None
        self.web_socket: Optional[any] = None
        self.web_socket_opened: bool = False
        self.peer_connection: Optional[RTCPeerConnection] = None

    def webrtc_peer_connection_factory(self):
        """Create and configure WebRTC peer connection using aiortc."""
        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Creating WebRTC peer connection..."
        )

        # Create RTCConfiguration with existing ICE servers
        config = RTCConfiguration(
            [RTCIceServer(urls=ice_server.urls) for ice_server in self.ice_servers]
        )

        # Create peer connection
        self.peer_connection = RTCPeerConnection(configuration=config)

        # Set up event handlers
        @self.peer_connection.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state is {self.peer_connection.connectionState}")

        @self.peer_connection.on("track")
        async def on_track(track):
            logger.info(f"Track received: {track.kind}")
            if track.kind == "video":
                # Handle video track
                pass
            elif track.kind == "audio":
                # Mute audio by default
                track.enabled = False

        return True if self.peer_connection else False

    async def handle_signaling_message(self, message):
        """Handle incoming signaling messages"""
        try:
            msg = json.loads(message)

            if msg.get("type") == "offer":
                offer = RTCSessionDescription(sdp=msg["sdp"], type=msg["type"])
                await self.peer_connection.setRemoteDescription(offer)

                answer = await self.peer_connection.createAnswer()
                await self.peer_connection.setLocalDescription(answer)

                response = {
                    "type": "answer",
                    "sdp": self.peer_connection.localDescription.sdp,
                }
                await self.web_socket.send(json.dumps(response))

            elif msg.get("type") == "ice-candidate":
                if msg.get("candidate"):
                    await self.peer_connection.addIceCandidate(msg["candidate"])
        except Exception as e:
            logger.error(f"Signaling error: {e}")
            raise

    async def setup_peer_connection_handlers(self):
        """Setup WebRTC peer connection event handlers."""

        @self.peer_connection.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(
                f"ICE connection state: {self.peer_connection.iceConnectionState}"
            )

        @self.peer_connection.on("icegatheringstatechange")
        async def on_icegatheringstatechange():
            logger.info(
                f"ICE gathering state: {self.peer_connection.iceGatheringState}"
            )

        @self.peer_connection.on("icecandidate")
        async def on_icecandidate(event):
            if event.candidate:
                candidate_data = {
                    "type": "ice-candidate",
                    "candidate": event.candidate.sdp,
                    "sdpMid": event.candidate.sdpMid,
                    "sdpMLineIndex": event.candidate.sdpMLineIndex,
                }
                await self.web_socket.send(json.dumps(candidate_data))

    async def complete_start_webrtc_connection(self):
        """Complete WebRTC connection setup with configuration."""
        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Creating WebRTC connection..."
        )

        # Initialize peer connection
        success = self.webrtc_peer_connection_factory()
        if not success:
            raise Exception("Failed to create peer connection")

        # Setup connection handlers
        await self.setup_peer_connection_handlers()

        # Wait for signaling to complete
        try:
            while self.peer_connection.connectionState != "connected":
                await asyncio.sleep(0.1)
        except Exception as e:
            logger.error(f"Connection failed: {str(e)}")
            raise

    def start_webrtc_connection(self, mac: str, access_token: str, model: str):
        """Start WebRTC connection with given parameters."""
        # Validate required parameters
        required = {"mac": mac, "access_token": access_token, "model": model}
        if any(not v for v in required.values()):
            raise ValueError("All parameters are required")

        # Check connection state
        if self.state == WebRtcState.CONNECTED:
            logger.info(
                f"[{mac}] [{self.session_id_crc32}] Already connected. Do nothing."
            )
            return
        elif self.state != WebRtcState.DISCONNECTED:
            logger.error(
                f"[{mac}] [{self.session_id_crc32}] Client is in {self.state} state. "
                "Cannot make new connection."
            )
            return

        # Update state and generate session ID
        self.state = WebRtcState.CONNECTING
        self.session_id = str(uuid.uuid4())
        self.session_id_crc32 = format(
            zlib.crc32(self.session_id.encode()) & 0xFFFFFFFF, "08x"
        )[4:]

        logger.info(
            f"[{mac}] [{self.session_id_crc32}] Connecting WebRTC w/ session "
            f"'{self.session_id}'..."
        )

        # Store connection properties
        self.mac = mac
        self.access_token = access_token
        self.model = model

        self.create_webrtc_session(mac, model)

    def add_stream_to_peer(self):
        """Add media stream to local peer connection."""
        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Adding stream to local peer..."
        )

        try:
            # Create local media stream
            local_media_stream = self.peer_connection_factory.create_local_media_stream(
                "KvsLocalMediaStream"
            )

            if not local_media_stream:
                self._handle_failure(success=False)
                return

            # Add audio track
            if not local_media_stream.add_track(self.local_audio_track):
                self._handle_failure(success=False)
                return

            # Add audio tracks to peer connection
            audio_tracks = local_media_stream.audio_tracks
            if audio_tracks and len(audio_tracks) > 0:
                self.peer_connection.add_track(audio_tracks[0], [local_media_stream.id])

        except Exception as e:
            logger.error(
                f"[{self.mac}] [{self.session_id_crc32}] Error adding stream: {str(e)}"
            )
            self._handle_failure(success=False)

    def create_webrtc_session(self, device_id: str, device_model: str):
        """Create WebRTC session with device."""
        # self._update_statistics_step(WebRtcConnectStep.CREATE_SESSION)

        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Requesting signaling/stun/turn URLs..."
        )

        # try:
        # Get camera stream info
        stream = get_camera_stream(
            auth_info=WyzeCredential(access_token=self.access_token),
            camera=WyzeCamera(
                mac=device_id,
                product_model=device_model,
                camera_info=None,
                dtls=None,
                parent_dtls=None,
                enr=None,
                firmware_ver=None,
                ip=None,
                nickname=None,
                p2p_id=None,
                p2p_type=None,
                parent_enr=None,
                parent_mac=None,
                thumbnail=None,
                timezone_name=None,
            ),
        )

        if not stream:
            logger.error(
                f"[{self.mac}] [{self.session_id_crc32}] Failed to get signaling/stun/turn URLs"
            )
            return

        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Response w/ signaling/stun/turn "
        )

        # Set tokens and URLs
        params = stream.params
        self.webrtc_context.signaling_token = params.auth_token or ""
        self.webrtc_context.signaling_url = params.signaling_url or ""

        # Configure ICE servers
        ice_servers = []
        for ice_service in params.ice_servers or []:
            server = IceServer(
                **{
                    "url": ice_service.url,
                    "username": ice_service.username or "",
                    "credential": ice_service.credential or "",
                }
            )
            if server:
                logger.info(
                    f"[{self.mac}] [{self.session_id_crc32}] Added ICE server '{ice_service.url}'"
                )
            else:
                logger.error(
                    f"[{self.mac}] [{self.session_id_crc32}] Failed to add ICE server"
                )

            ice_servers.append(server)

        logger.info(
            f"[{self.mac}] [{self.session_id_crc32}] Added {len(ice_servers)} ICE servers"
        )
        self.webrtc_context.ice_servers = ice_servers

        # Update state and start connection
        self.state = WebRtcState.CONNECTED
        self.complete_start_webrtc_connection()

    async def connect_websocket(self):
        """Initialize WebSocket connection"""
        try:
            # Your existing WebSocket connection code here
            self.web_socket_opened = True
            await self.setup_peer_connection()
        except Exception as e:
            logger.error(f"WebSocket connection failed: {e}")
            raise

    async def handle_video_track(self, track):
        """Handle incoming video track"""
        logger.info(f"Handling video track: {track.kind}")
        # Your video processing logic here
        self.video_track = track

    async def cleanup(self):
        """Clean up WebRTC and WebSocket connections"""
        if self.peer_connection:
            await self.peer_connection.close()
        if self.web_socket:
            await self.web_socket.close()
        self.web_socket_opened = False

    async def __aenter__(self):
        """Context manager entry"""
        await self.connect_websocket()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit"""
        await self.cleanup()
