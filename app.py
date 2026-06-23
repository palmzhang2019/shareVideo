import asyncio
import contextlib
import hashlib
import json
import mimetypes
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

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
CLIENT_UPLOAD_CHUNK = 8 * 1024 * 1024

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
    stream_manifest: str | None = None
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


@dataclass
class UploadSession:
    upload_id: str
    room_id: str
    filename: str
    safe_filename: str
    total_bytes: int
    last_modified: int
    created_at: int
    updated_at: int


app = FastAPI(title="Shared Cinema")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

ROOMS: dict[str, RoomState] = {}
CLIENTS: dict[WebSocket, ClientMeta] = {}
UPLOAD_LOCKS: dict[str, asyncio.Lock] = {}
HLS_MASTER_PLAYLIST = "master.m3u8"


@dataclass(frozen=True)
class HlsVariant:
    name: str
    target_height: int
    video_bitrate_kbps: int
    maxrate_kbps: int
    bufsize_kbps: int
    audio_bitrate_kbps: int


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


def get_room_hls_dir(room_id: str, upload_id: str) -> Path:
    return get_room_dir(room_id) / f"hls-{upload_id}"


def build_upload_id(filename: str, total_bytes: int, last_modified: int) -> str:
    digest = hashlib.sha256(
        f"{sanitize_filename(filename)}:{total_bytes}:{last_modified}".encode("utf-8")
    ).hexdigest()
    return digest[:24]


def get_upload_temp_path(room_id: str, upload_id: str) -> Path:
    return get_room_dir(room_id) / f".upload-{upload_id}.part"


def get_upload_meta_path(room_id: str, upload_id: str) -> Path:
    return get_room_dir(room_id) / f".upload-{upload_id}.json"


def upload_session_to_payload(session: UploadSession) -> dict[str, Any]:
    return {
        "upload_id": session.upload_id,
        "filename": session.filename,
        "safe_filename": session.safe_filename,
        "total_bytes": session.total_bytes,
        "last_modified": session.last_modified,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
    }


def load_upload_session(room_id: str, upload_id: str) -> UploadSession | None:
    meta_path = get_upload_meta_path(room_id, upload_id)
    if not meta_path.exists():
        return None

    try:
        payload = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        with contextlib.suppress(OSError):
            meta_path.unlink()
        return None

    try:
        return UploadSession(
            upload_id=str(payload["upload_id"]),
            room_id=room_id,
            filename=str(payload["filename"]),
            safe_filename=str(payload["safe_filename"]),
            total_bytes=int(payload["total_bytes"]),
            last_modified=int(payload.get("last_modified", 0)),
            created_at=int(payload["created_at"]),
            updated_at=int(payload["updated_at"]),
        )
    except (KeyError, TypeError, ValueError):
        with contextlib.suppress(OSError):
            meta_path.unlink()
        return None


def save_upload_session(session: UploadSession) -> None:
    meta_path = get_upload_meta_path(session.room_id, session.upload_id)
    meta_path.write_text(
        json.dumps(upload_session_to_payload(session), ensure_ascii=True),
        encoding="utf-8",
    )


def current_upload_size(session: UploadSession) -> int:
    temp_path = get_upload_temp_path(session.room_id, session.upload_id)
    if not temp_path.exists():
        return 0
    return temp_path.stat().st_size


def delete_upload_artifacts(room_id: str, upload_id: str) -> None:
    temp_path = get_upload_temp_path(room_id, upload_id)
    meta_path = get_upload_meta_path(room_id, upload_id)
    with contextlib.suppress(OSError):
        temp_path.unlink()
    with contextlib.suppress(OSError):
        meta_path.unlink()


def clear_other_upload_artifacts(room_id: str, keep_upload_id: str | None = None) -> None:
    room_dir = get_room_dir(room_id)
    if not room_dir.exists():
        return

    for path in room_dir.iterdir():
        if not path.name.startswith(".upload-"):
            continue

        if keep_upload_id and path.name.startswith(f".upload-{keep_upload_id}."):
            continue

        with contextlib.suppress(OSError):
            path.unlink()


def get_room_upload_lock(room_id: str) -> asyncio.Lock:
    return UPLOAD_LOCKS.setdefault(room_id, asyncio.Lock())


def get_room_video_path(room: RoomState) -> Path | None:
    if not room.video_filename:
        return None
    return get_room_dir(room.room_id) / room.video_filename


def get_room_manifest_path(room: RoomState) -> Path | None:
    if not room.stream_manifest:
        return None
    return get_room_dir(room.room_id) / room.stream_manifest


def guess_video_media_type(path: Path, filename: str | None = None) -> str:
    suffix = path.suffix.lower()
    if suffix == ".m3u8":
        return "application/vnd.apple.mpegurl"
    if suffix == ".ts":
        return "video/mp2t"

    media_type, _ = mimetypes.guess_type(filename or path.name)
    return media_type or "application/octet-stream"


FASTSTART_EXTENSIONS = {".mp4", ".m4v", ".mov"}


async def optimize_for_streaming(path: Path) -> None:
    """Move the MP4/MOV moov atom to the front of the file (faststart).

    iOS Safari must read the moov atom before it can start playback. Many
    recorders/exporters write it at the end of the file, which forces the
    browser to download the whole file first — over a slow cross-border link
    that effectively never finishes, so the page loads but the video never
    plays. A stream copy is cheap (no re-encode) and fixes this. If ffmpeg is
    not installed or the remux fails for any reason, the original file is left
    untouched.
    """
    if path.suffix.lower() not in FASTSTART_EXTENSIONS:
        return

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return

    optimized = path.with_name(f".faststart-{uuid.uuid4().hex}{path.suffix}")
    try:
        process = await asyncio.create_subprocess_exec(
            ffmpeg,
            "-v", "error",
            "-y",
            "-i", str(path),
            "-c", "copy",
            "-movflags", "+faststart",
            str(optimized),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        if (
            process.returncode == 0
            and optimized.exists()
            and optimized.stat().st_size > 0
        ):
            os.replace(optimized, path)
    except Exception:
        pass
    finally:
        with contextlib.suppress(OSError):
            if optimized.exists():
                optimized.unlink()


def remove_path(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
        return

    with contextlib.suppress(OSError):
        path.unlink()


def remove_stale_room_media(room_dir: Path, keep_paths: set[Path]) -> None:
    for path in room_dir.iterdir():
        if path in keep_paths or path.name.startswith(".upload-"):
            continue
        remove_path(path)


def even_dimension(value: int) -> int:
    return max(2, value - (value % 2))


def choose_hls_variants(source_height: int) -> list[HlsVariant]:
    normalized_height = even_dimension(max(source_height, 360))

    if normalized_height >= 720:
        candidates = [
            HlsVariant("360p", 360, 700, 770, 1050, 96),
            HlsVariant("720p", 720, 2200, 2420, 3300, 128),
        ]
    elif normalized_height >= 480:
        candidates = [
            HlsVariant("360p", 360, 700, 770, 1050, 96),
            HlsVariant(f"{normalized_height}p", normalized_height, 1400, 1540, 2100, 128),
        ]
    else:
        candidates = [
            HlsVariant(f"{normalized_height}p", normalized_height, 650, 715, 975, 96),
        ]

    unique_variants: dict[int, HlsVariant] = {}
    for variant in candidates:
        unique_variants[variant.target_height] = variant
    return sorted(unique_variants.values(), key=lambda item: item.target_height)


def scaled_dimensions(
    source_width: int, source_height: int, target_height: int
) -> tuple[int, int]:
    if source_width <= 0 or source_height <= 0:
        return (640, target_height)

    if source_height <= target_height:
        return (even_dimension(source_width), even_dimension(source_height))

    scale = target_height / source_height
    return (
        even_dimension(int(source_width * scale)),
        even_dimension(int(source_height * scale)),
    )


async def probe_media(path: Path) -> tuple[int, int, bool]:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return (0, 0, False)

    process = await asyncio.create_subprocess_exec(
        ffprobe,
        "-v",
        "error",
        "-show_streams",
        "-of",
        "json",
        str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate()
    if process.returncode != 0:
        return (0, 0, False)

    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, TypeError):
        return (0, 0, False)

    streams = payload.get("streams", [])
    width = 0
    height = 0
    has_audio = False
    for stream in streams:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not width and not height:
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
        elif codec_type == "audio":
            has_audio = True

    return (width, height, has_audio)


async def transcode_hls_variant(
    input_path: Path,
    variant_dir: Path,
    variant: HlsVariant,
    has_audio: bool,
) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to generate HLS output.")

    variant_dir.mkdir(parents=True, exist_ok=True)
    playlist_path = variant_dir / "index.m3u8"
    segment_pattern = variant_dir / "segment_%03d.ts"
    scale_filter = (
        f"scale=w=-2:h={variant.target_height}:force_original_aspect_ratio=decrease"
    )

    args = [
        ffmpeg,
        "-v",
        "error",
        "-y",
        "-i",
        str(input_path),
        "-map",
        "0:v:0",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-profile:v",
        "high",
        "-level",
        "4.1",
        "-vf",
        scale_filter,
        "-g",
        "48",
        "-keyint_min",
        "48",
        "-sc_threshold",
        "0",
        "-b:v",
        f"{variant.video_bitrate_kbps}k",
        "-maxrate",
        f"{variant.maxrate_kbps}k",
        "-bufsize",
        f"{variant.bufsize_kbps}k",
    ]

    if has_audio:
        args.extend(
            [
                "-map",
                "0:a:0",
                "-c:a",
                "aac",
                "-b:a",
                f"{variant.audio_bitrate_kbps}k",
                "-ac",
                "2",
            ]
        )
    else:
        args.append("-an")

    args.extend(
        [
            "-f",
            "hls",
            "-hls_time",
            "4",
            "-hls_playlist_type",
            "vod",
            "-hls_list_size",
            "0",
            "-hls_flags",
            "independent_segments",
            "-hls_segment_filename",
            str(segment_pattern),
            str(playlist_path),
        ]
    )

    process = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await process.wait()
    if process.returncode != 0 or not playlist_path.exists():
        raise RuntimeError(f"Failed to generate HLS variant {variant.name}.")


async def generate_hls_stream(input_path: Path, room_id: str, upload_id: str) -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return None

    source_width, source_height, has_audio = await probe_media(input_path)
    variants = choose_hls_variants(source_height)
    if not variants:
        return None

    stream_root = get_room_hls_dir(room_id, upload_id)
    if stream_root.exists():
        shutil.rmtree(stream_root, ignore_errors=True)
    stream_root.mkdir(parents=True, exist_ok=True)

    variant_entries: list[tuple[HlsVariant, tuple[int, int]]] = []
    try:
        for variant in variants:
            variant_dir = stream_root / variant.name
            await transcode_hls_variant(input_path, variant_dir, variant, has_audio)
            variant_entries.append(
                (variant, scaled_dimensions(source_width, source_height, variant.target_height))
            )

        master_playlist = stream_root / HLS_MASTER_PLAYLIST
        lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for variant, (width, height) in variant_entries:
            bandwidth_kbps = variant.video_bitrate_kbps
            if has_audio:
                bandwidth_kbps += variant.audio_bitrate_kbps
            average_bandwidth = int(bandwidth_kbps * 0.85 * 1000)
            lines.append(
                (
                    "#EXT-X-STREAM-INF:"
                    f"BANDWIDTH={bandwidth_kbps * 1000},"
                    f"AVERAGE-BANDWIDTH={average_bandwidth},"
                    f"RESOLUTION={width}x{height}"
                )
            )
            lines.append(f"{variant.name}/index.m3u8")

        master_playlist.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return f"{stream_root.name}/{HLS_MASTER_PLAYLIST}"
    except Exception:
        shutil.rmtree(stream_root, ignore_errors=True)
        return None


def current_fallback_video_url(room: RoomState) -> str | None:
    if not room.video_ready:
        return None

    video_path = get_room_video_path(room)
    if not video_path or not video_path.exists():
        return None

    version = video_path.stat().st_mtime_ns
    return f"/room/{quote(room.room_id)}/video?v={version}"


def current_video_payload(room: RoomState) -> dict[str, str | None]:
    fallback_video_url = current_fallback_video_url(room)
    stream_path = get_room_manifest_path(room)

    if stream_path and stream_path.exists() and room.stream_manifest:
        version = stream_path.stat().st_mtime_ns
        return {
            "video_url": f"/room/{quote(room.room_id)}/stream/{room.stream_manifest}?v={version}",
            "video_type": "application/vnd.apple.mpegurl",
            "fallback_video_url": fallback_video_url,
        }

    video_path = get_room_video_path(room)
    if video_path and video_path.exists():
        return {
            "video_url": fallback_video_url,
            "video_type": guess_video_media_type(video_path, room.video_filename),
            "fallback_video_url": None,
        }

    return {
        "video_url": None,
        "video_type": None,
        "fallback_video_url": None,
    }


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
        **current_video_payload(room),
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

        manifests = [
            path
            for path in room_dir.glob(f"hls-*/{HLS_MASTER_PLAYLIST}")
            if path.is_file()
        ]
        if manifests:
            latest_manifest = max(manifests, key=lambda item: item.stat().st_mtime_ns)
            room.stream_manifest = str(latest_manifest.relative_to(room_dir))

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
async def init_upload(room_id: str, request: Request) -> JSONResponse:
    ensure_room(room_id)
    payload = await request.json()
    filename = str(payload.get("filename", "")).strip()

    try:
        total_bytes = int(payload.get("size", 0))
    except (TypeError, ValueError):
        total_bytes = 0

    try:
        last_modified = int(payload.get("last_modified", 0))
    except (TypeError, ValueError):
        last_modified = 0

    if not filename:
        raise HTTPException(status_code=400, detail="Missing filename.")
    if total_bytes <= 0:
        raise HTTPException(status_code=400, detail="Invalid file size.")

    safe_filename = sanitize_filename(filename)
    upload_id = build_upload_id(filename, total_bytes, last_modified)
    room_dir = get_room_dir(room_id)
    room_dir.mkdir(parents=True, exist_ok=True)
    lock = get_room_upload_lock(room_id)

    async with lock:
        clear_other_upload_artifacts(room_id, keep_upload_id=upload_id)
        session = load_upload_session(room_id, upload_id)
        if session is None:
            created_at = now_ms()
            session = UploadSession(
                upload_id=upload_id,
                room_id=room_id,
                filename=filename,
                safe_filename=safe_filename,
                total_bytes=total_bytes,
                last_modified=last_modified,
                created_at=created_at,
                updated_at=created_at,
            )
        else:
            session.updated_at = now_ms()

        uploaded_bytes = current_upload_size(session)
        if uploaded_bytes > session.total_bytes:
            delete_upload_artifacts(room_id, upload_id)
            uploaded_bytes = 0
            created_at = now_ms()
            session = UploadSession(
                upload_id=upload_id,
                room_id=room_id,
                filename=filename,
                safe_filename=safe_filename,
                total_bytes=total_bytes,
                last_modified=last_modified,
                created_at=created_at,
                updated_at=created_at,
            )

        save_upload_session(session)

    return JSONResponse(
        {
            "ok": True,
            "upload_id": upload_id,
            "uploaded_bytes": uploaded_bytes,
            "chunk_size": CLIENT_UPLOAD_CHUNK,
            "total_bytes": total_bytes,
            "filename": safe_filename,
        }
    )


@app.post("/room/{room_id}/upload/{upload_id}/chunk")
async def upload_video_chunk(
    room_id: str, upload_id: str, request: Request
) -> JSONResponse:
    ensure_room(room_id)

    try:
        expected_offset = int(request.headers.get("x-upload-offset", "0"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid upload offset.") from None

    try:
        chunk_bytes = int(request.headers.get("x-upload-chunk-bytes", "0"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid chunk size.") from None

    if expected_offset < 0:
        raise HTTPException(status_code=400, detail="Invalid upload offset.")
    if chunk_bytes <= 0:
        raise HTTPException(status_code=400, detail="Invalid chunk size.")

    lock = get_room_upload_lock(room_id)
    async with lock:
        session = load_upload_session(room_id, upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Upload session not found.")

        temp_path = get_upload_temp_path(room_id, upload_id)
        uploaded_bytes = current_upload_size(session)
        if expected_offset != uploaded_bytes:
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Upload offset mismatch.",
                    "uploaded_bytes": uploaded_bytes,
                },
            )

        bytes_written = 0
        try:
            async with aiofiles.open(temp_path, "ab") as file_handle:
                async for chunk in request.stream():
                    if not chunk:
                        continue

                    if uploaded_bytes + bytes_written + len(chunk) > session.total_bytes:
                        raise HTTPException(status_code=400, detail="Chunk exceeds file size.")

                    for start in range(0, len(chunk), MAX_UPLOAD_CHUNK):
                        piece = chunk[start : start + MAX_UPLOAD_CHUNK]
                        if piece:
                            await file_handle.write(piece)
                            bytes_written += len(piece)

                await file_handle.flush()
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Upload failed: {exc}") from exc

        if bytes_written != chunk_bytes:
            raise HTTPException(status_code=400, detail="Incomplete chunk payload.")

        uploaded_bytes += bytes_written
        session.updated_at = now_ms()
        save_upload_session(session)

    return JSONResponse(
        {
            "ok": True,
            "upload_id": upload_id,
            "uploaded_bytes": uploaded_bytes,
            "received_bytes": bytes_written,
            "done": uploaded_bytes >= session.total_bytes,
        }
    )


@app.post("/room/{room_id}/upload/{upload_id}/complete")
async def complete_upload(room_id: str, upload_id: str) -> JSONResponse:
    room = ensure_room(room_id)
    room_dir = get_room_dir(room_id)
    lock = get_room_upload_lock(room_id)

    async with lock:
        session = load_upload_session(room_id, upload_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Upload session not found.")

        temp_path = get_upload_temp_path(room_id, upload_id)
        final_path = room_dir / session.safe_filename
        uploaded_bytes = current_upload_size(session)
        if uploaded_bytes != session.total_bytes:
            raise HTTPException(
                status_code=409,
                detail="Upload is incomplete.",
            )

        try:
            os.replace(temp_path, final_path)
            delete_upload_artifacts(room_id, upload_id)
            clear_other_upload_artifacts(room_id)
            remove_stale_room_media(room_dir, keep_paths={final_path})

            await optimize_for_streaming(final_path)
            stream_manifest = await generate_hls_stream(final_path, room_id, upload_id)

            room.video_filename = session.safe_filename
            room.stream_manifest = stream_manifest
            room.video_ready = True
            room.is_playing = False
            room.position = 0.0
            room.updated_at = now_ms()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Finalize failed: {exc}") from exc

    video_payload = current_video_payload(room)
    await broadcast_json(room, {"type": "video_ready", **video_payload})
    await broadcast_json(room, serialize_state(room))
    return JSONResponse(
        {
            "ok": True,
            "room_id": room_id,
            **video_payload,
            "bytes_written": uploaded_bytes,
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
        media_type=guess_video_media_type(video_path, room.video_filename),
        filename=room.video_filename,
        content_disposition_type="inline",
        headers={
            # The URL is versioned with ?v=<mtime>, so the bytes are immutable
            # for a given URL. Letting iOS cache and resume via Range requests
            # is far more reliable on a slow link than re-downloading each time.
            "Cache-Control": "public, max-age=31536000, immutable",
            "Accept-Ranges": "bytes",
        },
    )


@app.get("/room/{room_id}/stream/{stream_path:path}")
async def stream_room_hls_asset(room_id: str, stream_path: str) -> FileResponse:
    room = ensure_room(room_id)
    if not room.video_ready:
        raise HTTPException(status_code=404, detail="No video uploaded for this room.")
    if not stream_path.startswith("hls-"):
        raise HTTPException(status_code=404, detail="Asset not found.")

    room_dir = get_room_dir(room_id).resolve()
    asset_path = (room_dir / stream_path).resolve()
    try:
        asset_path.relative_to(room_dir)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Asset not found.") from exc

    if not asset_path.exists() or not asset_path.is_file() or asset_path.name.startswith("."):
        raise HTTPException(status_code=404, detail="Asset not found.")

    return FileResponse(
        path=asset_path,
        media_type=guess_video_media_type(asset_path),
        filename=asset_path.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
        },
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
