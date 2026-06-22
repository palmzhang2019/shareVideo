import asyncio
import contextlib
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

import aiofiles
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from db import add_danmaku, get_danmaku_for_room, init_db


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
VIDEOS_DIR = DATA_DIR / "videos"
DATABASE_PATH = DATA_DIR / "shared_cinema.sqlite3"
MAX_UPLOAD_CHUNK = 1024 * 1024

ADJECTIVES = [
    "Swift",
    "Quiet",
    "Bright",
    "Amber",
    "Silver",
    "Lucky",
    "Mellow",
    "Nova",
    "Sunny",
    "Crisp",
]
ANIMALS = [
    "Fox",
    "Otter",
    "Panda",
    "Falcon",
    "Dolphin",
    "Koala",
    "Tiger",
    "Rabbit",
    "Whale",
    "Sparrow",
]


@dataclass
class RoomState:
    room_id: str
    video_filename: str | None = None
    video_ready: bool = False
    is_playing: bool = False
    position: float = 0.0
    updated_at: float = 0.0
    clients: set[WebSocket] = field(default_factory=set)


@dataclass
class ClientMeta:
    room_id: str
    client_id: str
    nickname: str


app = FastAPI(title="Shared Cinema")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ROOMS: dict[str, RoomState] = {}
CLIENTS: dict[WebSocket, ClientMeta] = {}
UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}


def now_ms() -> int:
    return int(time.time() * 1000)


def sanitize_filename(filename: str) -> str:
    candidate = Path(filename).name.strip()
    if not candidate:
        return f"video-{uuid.uuid4().hex}.bin"

    allowed = []
    for char in candidate:
        if char.isalnum() or char in {".", "_", "-"}:
            allowed.append(char)
        else:
            allowed.append("_")

    cleaned = "".join(allowed).strip("._")
    return cleaned or f"video-{uuid.uuid4().hex}.bin"


def generate_room_id() -> str:
    return uuid.uuid4().hex[:10]


def generate_nickname() -> str:
    adjective = ADJECTIVES[uuid.uuid4().int % len(ADJECTIVES)]
    animal = ANIMALS[(uuid.uuid4().int >> 4) % len(ANIMALS)]
    return f"{adjective}{animal}"


def get_room_dir(room_id: str) -> Path:
    return VIDEOS_DIR / room_id


def get_room_video_path(room: RoomState) -> Path | None:
    if not room.video_filename:
        return None
    return get_room_dir(room.room_id) / room.video_filename


def current_video_url(room: RoomState) -> str | None:
    if not room.video_ready:
        return None

    video_path = get_room_video_path(room)
    if not video_path or not video_path.exists():
        return None

    version = video_path.stat().st_mtime_ns
    return f"/room/{quote(room.room_id)}/video?v={version}"


def effective_position(room: RoomState, reference_ms: int | None = None) -> float:
    reference = reference_ms if reference_ms is not None else now_ms()
    if not room.is_playing:
        return max(room.position, 0.0)

    delta_seconds = max(reference - room.updated_at, 0) / 1000
    return max(room.position + delta_seconds, 0.0)


def serialize_state(room: RoomState, reference_ms: int | None = None) -> dict[str, Any]:
    server_time = reference_ms if reference_ms is not None else now_ms()
    return {
        "type": "state",
        "is_playing": room.is_playing,
        "position": round(effective_position(room, server_time), 3),
        "server_time": server_time,
        "video_ready": room.video_ready,
        "video_url": current_video_url(room),
    }


def ensure_room(room_id: str) -> RoomState:
    room = ROOMS.get(room_id)
    if room:
        return room

    room = RoomState(room_id=room_id, updated_at=now_ms())
    room_dir = get_room_dir(room_id)
    if room_dir.exists():
        files = [
            path
            for path in room_dir.iterdir()
            if path.is_file() and not path.name.startswith(".")
        ]
        if files:
            latest = max(files, key=lambda item: item.stat().st_mtime_ns)
            room.video_filename = latest.name
            room.video_ready = True

    ROOMS[room_id] = room
    return room


async def broadcast_json(room: RoomState, payload: dict[str, Any]) -> None:
    disconnected: list[WebSocket] = []
    for websocket in tuple(room.clients):
        try:
            await websocket.send_json(payload)
        except Exception:
            disconnected.append(websocket)

    for websocket in disconnected:
        room.clients.discard(websocket)
        CLIENTS.pop(websocket, None)


async def broadcast_viewers(room: RoomState) -> None:
    await broadcast_json(room, {"type": "viewers", "count": len(room.clients)})


async def update_room_state(room: RoomState, is_playing: bool | None, position: float) -> None:
    current_ms = now_ms()
    room.position = max(position, 0.0)
    if is_playing is not None:
        room.is_playing = is_playing
    room.updated_at = current_ms
    await broadcast_json(room, serialize_state(room, current_ms))


async def remove_stale_file(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


@app.on_event("startup")
async def on_startup() -> None:
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    await init_db(DATABASE_PATH)


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"request": request},
    )


@app.post("/api/rooms")
async def create_room() -> JSONResponse:
    room_id = generate_room_id()
    while room_id in ROOMS:
        room_id = generate_room_id()

    ensure_room(room_id)
    return JSONResponse({"room_id": room_id})


@app.get("/room/{room_id}", response_class=HTMLResponse)
async def room_page(request: Request, room_id: str) -> HTMLResponse:
    ensure_room(room_id)
    return templates.TemplateResponse(
        request=request,
        name="room.html",
        context={
            "request": request,
            "room_id": room_id,
            "share_url": str(request.url),
        },
    )


@app.post("/room/{room_id}/upload")
async def upload_video(room_id: str, request: Request) -> JSONResponse:
    room = ensure_room(room_id)
    filename = unquote(request.headers.get("x-filename", ""))
    safe_filename = sanitize_filename(filename)
    room_dir = get_room_dir(room_id)
    room_dir.mkdir(parents=True, exist_ok=True)
    temp_path = room_dir / f".upload-{uuid.uuid4().hex}.part"
    final_path = room_dir / safe_filename
    lock = UPLOAD_LOCKS.setdefault(room_id, asyncio.Lock())
    total_bytes = 0

    async with lock:
        try:
            async with aiofiles.open(temp_path, "wb") as file_handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue

                    for start in range(0, len(chunk), MAX_UPLOAD_CHUNK):
                        piece = chunk[start : start + MAX_UPLOAD_CHUNK]
                        if piece:
                            await file_handle.write(piece)
                            total_bytes += len(piece)

            os.replace(temp_path, final_path)

            for path in room_dir.iterdir():
                if path.is_file() and path != final_path:
                    with contextlib.suppress(FileNotFoundError):
                        path.unlink()

            room.video_filename = safe_filename
            room.video_ready = True
            room.is_playing = False
            room.position = 0.0
            room.updated_at = now_ms()
        except Exception as exc:
            await remove_stale_file(temp_path)
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

    video_url = current_video_url(room)
    await broadcast_json(room, {"type": "video_ready", "video_url": video_url})
    await broadcast_json(room, serialize_state(room))
    return JSONResponse(
        {
            "ok": True,
            "room_id": room_id,
            "video_url": video_url,
            "bytes_written": total_bytes,
        }
    )


@app.get("/room/{room_id}/video")
async def stream_room_video(room_id: str) -> FileResponse:
    room = ensure_room(room_id)
    video_path = get_room_video_path(room)
    if not room.video_ready or not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail="No video uploaded for this room.")

    return FileResponse(
        path=video_path,
        filename=room.video_filename,
        headers={"Cache-Control": "no-store"},
    )


@app.get("/room/{room_id}/danmaku")
async def room_danmaku(room_id: str) -> JSONResponse:
    ensure_room(room_id)
    items = await get_danmaku_for_room(DATABASE_PATH, room_id)
    return JSONResponse(items)


@app.websocket("/room/{room_id}/ws")
async def room_websocket(websocket: WebSocket, room_id: str) -> None:
    await websocket.accept()
    room = ensure_room(room_id)

    client_meta = ClientMeta(
        room_id=room_id,
        client_id=str(uuid.uuid4()),
        nickname=generate_nickname(),
    )
    CLIENTS[websocket] = client_meta
    room.clients.add(websocket)

    await websocket.send_json(
        {
            "type": "welcome",
            "client_id": client_meta.client_id,
            "nickname": client_meta.nickname,
        }
    )
    await websocket.send_json(serialize_state(room))
    await broadcast_viewers(room)

    try:
        while True:
            payload = await websocket.receive_json()
            message_type = payload.get("type")

            if message_type in {"play", "pause", "seek"}:
                try:
                    position = float(payload.get("position", 0.0))
                except (TypeError, ValueError):
                    continue

                if message_type == "play":
                    await update_room_state(room, True, position)
                elif message_type == "pause":
                    await update_room_state(room, False, position)
                else:
                    await update_room_state(room, None, position)

            elif message_type == "danmaku":
                text = str(payload.get("text", "")).strip()
                if not text:
                    continue

                try:
                    video_time = max(float(payload.get("video_time", 0.0)), 0.0)
                except (TypeError, ValueError):
                    continue

                created_at = now_ms()
                danmaku_id = await add_danmaku(
                    DATABASE_PATH,
                    room_id=room_id,
                    text=text,
                    video_time=video_time,
                    sender_id=client_meta.client_id,
                    created_at=created_at,
                )
                await broadcast_json(
                    room,
                    {
                        "type": "danmaku",
                        "id": danmaku_id,
                        "text": text,
                        "video_time": round(video_time, 3),
                        "sender_id": client_meta.client_id,
                        "server_time": created_at,
                    },
                )
    except WebSocketDisconnect:
        pass
    finally:
        room.clients.discard(websocket)
        CLIENTS.pop(websocket, None)
        await broadcast_viewers(room)
