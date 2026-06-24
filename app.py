import asyncio
import contextlib
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
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

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "sharevideo.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(
            LOG_FILE,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        ),
    ],
)
logger = logging.getLogger("sharevideo")

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
    media_upload_id: str | None = None
    video_filename: str | None = None
    stream_manifest: str | None = None
    stream_status: str = "none"
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
    ip: str = "unknown"
    user_agent: str = ""
    joined_at: int = 0


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
HLS_TASKS: dict[str, asyncio.Task[None]] = {}
BACKGROUND_TASKS: set[asyncio.Task[None]] = set()
HLS_PLAYLIST = "index.m3u8"


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


def get_client_ip(conn: Request | WebSocket) -> str:
    """Best-effort real client IP, honoring a reverse proxy if present.

    When deployed behind nginx/Caddy/Cloudflare the socket peer is the proxy,
    so the viewer's real address only shows up in X-Forwarded-For / X-Real-IP.
    """
    forwarded = conn.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real = conn.headers.get("x-real-ip")
    if real:
        return real.strip()
    if conn.client:
        return conn.client.host
    return "unknown"


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


def choose_hls_variant(source_height: int) -> HlsVariant:
    normalized_height = even_dimension(max(source_height, 360))

    if normalized_height >= 720:
        return HlsVariant("720p", 720, 1600, 1760, 2400, 128)
    if normalized_height >= 480:
        return HlsVariant("480p", 480, 1100, 1210, 1650, 96)
    return HlsVariant(f"{normalized_height}p", normalized_height, 650, 715, 975, 96)


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


async def start_hls_transcode(
    input_path: Path,
    room_id: str,
    upload_id: str,
) -> tuple[asyncio.subprocess.Process, Path, Path]:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to generate HLS output.")

    _source_width, source_height, has_audio = await probe_media(input_path)
    variant = choose_hls_variant(source_height)
    stream_root = get_room_hls_dir(room_id, upload_id)
    if stream_root.exists():
        shutil.rmtree(stream_root, ignore_errors=True)
    stream_root.mkdir(parents=True, exist_ok=True)

    playlist_path = stream_root / HLS_PLAYLIST
    segment_pattern = stream_root / "segment_%05d.ts"
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
        "superfast",
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
            "2",
            # VOD, not a live/event playlist: clients are only switched to HLS
            # once transcoding has fully finished, so the manifest is complete
            # (ends with #EXT-X-ENDLIST) and hls.js loads it once instead of
            # reload-storming a growing playlist.
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
    return (process, playlist_path, stream_root)


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
        # The manifest URL is deliberately NOT versioned with mtime: the path
        # already contains the per-upload id (hls-<upload_id>/...), so it is
        # unique per upload yet stable across the processing -> ready transition.
        # A changing URL would make the client rebind (restarting playback) every
        # time the manifest grows. The manifest is served no-store, so the client
        # always revalidates and eventually sees #EXT-X-ENDLIST and stops polling.
        return {
            "video_url": f"/room/{quote(room.room_id)}/stream/{room.stream_manifest}",
            "video_type": "application/vnd.apple.mpegurl",
            "fallback_video_url": fallback_video_url,
            "stream_status": room.stream_status,
        }

    video_path = get_room_video_path(room)
    if video_path and video_path.exists():
        return {
            "video_url": fallback_video_url,
            "video_type": guess_video_media_type(video_path, room.video_filename),
            "fallback_video_url": None,
            "stream_status": room.stream_status,
        }

    return {
        "video_url": None,
        "video_type": None,
        "fallback_video_url": None,
        "stream_status": room.stream_status,
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
            for path in room_dir.glob(f"hls-*/{HLS_PLAYLIST}")
            if path.is_file()
        ]
        if manifests:
            latest_manifest = max(manifests, key=lambda item: item.stat().st_mtime_ns)
            room.stream_manifest = str(latest_manifest.relative_to(room_dir))
            room.stream_status = "ready"
        elif room.video_ready:
            room.stream_status = "none"

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


def cancel_room_hls_task(room_id: str) -> None:
    task = HLS_TASKS.pop(room_id, None)
    if task and not task.done():
        task.cancel()


async def finalize_hls_in_background(
    room_id: str, upload_id: str, source_path: Path, video_filename: str
) -> None:
    stream_manifest: str | None = None
    stream_root: Path | None = None
    logger.info("hls transcode start room=%s file=%s", room_id, video_filename)
    try:
        process, playlist_path, stream_root = await start_hls_transcode(
            source_path,
            room_id,
            upload_id,
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("hls transcode failed to start room=%s file=%s", room_id, video_filename)
        process = None
        playlist_path = None

    try:
        if process and playlist_path and stream_root:
            relative_manifest = f"{stream_root.name}/{playlist_path.name}"

            # Wait for the transcode to finish. We do NOT expose the manifest
            # mid-flight: viewers stay on the MP4 fallback until HLS is a
            # complete VOD playlist, then switch once. Polling lets us abort
            # early if a newer upload supersedes this one.
            while True:
                room = ensure_room(room_id)
                if room.media_upload_id != upload_id or room.video_filename != video_filename:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                    await process.wait()
                    return

                try:
                    await asyncio.wait_for(process.wait(), timeout=0.5)
                    break
                except asyncio.TimeoutError:
                    continue

            if process.returncode == 0 and playlist_path.exists():
                stream_manifest = relative_manifest
    except asyncio.CancelledError:
        if process:
            with contextlib.suppress(ProcessLookupError):
                process.terminate()
            with contextlib.suppress(Exception):
                await process.wait()
        raise

    room = ensure_room(room_id)
    if room.media_upload_id != upload_id or room.video_filename != video_filename:
        return

    if stream_manifest:
        room.stream_manifest = stream_manifest
        room.stream_status = "ready"
        logger.info("hls transcode ready room=%s file=%s manifest=%s", room_id, video_filename, stream_manifest)
    else:
        room.stream_manifest = None
        room.stream_status = "failed"
        returncode = process.returncode if process else None
        logger.warning(
            "hls transcode failed room=%s file=%s returncode=%s; viewers fall back to MP4",
            room_id,
            video_filename,
            returncode,
        )
        if stream_root:
            shutil.rmtree(stream_root, ignore_errors=True)

    room.updated_at = now_ms()
    video_payload = current_video_payload(room)
    await broadcast_json(room, {"type": "video_ready", **video_payload})
    await broadcast_json(room, serialize_state(room))


def schedule_room_hls_generation(room: RoomState, upload_id: str, source_path: Path) -> None:
    cancel_room_hls_task(room.room_id)

    async def runner() -> None:
        try:
            await finalize_hls_in_background(
                room.room_id,
                upload_id,
                source_path,
                room.video_filename or "",
            )
        finally:
            current_task = HLS_TASKS.get(room.room_id)
            if current_task is asyncio.current_task():
                HLS_TASKS.pop(room.room_id, None)

    HLS_TASKS[room.room_id] = asyncio.create_task(runner())


def schedule_room_faststart(
    room_id: str, upload_id: str, video_filename: str, path: Path
) -> None:
    """Move the MP4 moov atom to the front in the background.

    This only affects the MP4 *fallback* download, so there is no reason to
    block the upload response (or the HLS transcode, which reads the same file
    via a separate fd) on it. We deliberately do NOT broadcast afterwards:
    clients no longer dedup on the fallback URL, so re-broadcasting only the
    changed fallback version would needlessly restart playback for everyone.
    """

    async def runner() -> None:
        try:
            await optimize_for_streaming(path)
        except Exception:
            logger.exception("faststart failed room=%s file=%s", room_id, video_filename)

    task = asyncio.create_task(runner())
    # Keep a reference so the task isn't garbage-collected mid-flight.
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


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

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        logger.info("ffmpeg detected (ffmpeg=%s ffprobe=%s); HLS transcoding enabled", ffmpeg, ffprobe)
    else:
        logger.warning(
            "ffmpeg/ffprobe NOT found (ffmpeg=%s ffprobe=%s). HLS transcoding and MP4 "
            "faststart are DISABLED -- viewers fall back to the raw upload, which on iOS "
            "or slow links may fail to play or require a full download. Install ffmpeg.",
            ffmpeg,
            ffprobe,
        )


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
    ffmpeg_available = shutil.which("ffmpeg") is not None

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
            cancel_room_hls_task(room_id)

            room.media_upload_id = upload_id
            room.video_filename = session.safe_filename
            room.stream_manifest = None
            room.stream_status = "processing" if ffmpeg_available else "none"
            room.video_ready = True
            room.is_playing = False
            room.position = 0.0
            room.updated_at = now_ms()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"Finalize failed: {exc}") from exc

    logger.info(
        "upload complete room=%s file=%s bytes=%d ffmpeg=%s",
        room_id,
        session.safe_filename,
        uploaded_bytes,
        ffmpeg_available,
    )

    # Run HLS transcode and MP4 faststart concurrently instead of waiting for
    # faststart first: HLS is the primary source, and faststart only matters for
    # the MP4 fallback, so blocking the slower encode behind it wastes time.
    if ffmpeg_available:
        schedule_room_hls_generation(room, upload_id, final_path)
    schedule_room_faststart(room_id, upload_id, session.safe_filename, final_path)

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
async def stream_room_video(room_id: str, request: Request) -> FileResponse:
    room = ensure_room(room_id)
    video_path = get_room_video_path(room)
    if not room.video_ready or not video_path or not video_path.exists():
        raise HTTPException(status_code=404, detail="No video uploaded for this room.")

    # Players issue many Range requests per playback; only log the opening one
    # (no Range, or a range starting at byte 0) so the log shows that a viewer
    # started the MP4 fallback without flooding on every chunk.
    range_header = request.headers.get("range", "")
    if not range_header or range_header.startswith("bytes=0-"):
        logger.info(
            "serve mp4-fallback room=%s ip=%s file=%s range=%r ua=%r",
            room_id,
            get_client_ip(request),
            room.video_filename,
            range_header,
            request.headers.get("user-agent", ""),
        )

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
async def stream_room_hls_asset(
    room_id: str, stream_path: str, request: Request
) -> FileResponse:
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

    is_manifest = asset_path.suffix == ".m3u8"

    # Log only manifest fetches, not every .ts segment, to keep the log readable
    # while still showing that a viewer actually started the HLS stream.
    if is_manifest:
        logger.info(
            "serve hls-manifest room=%s ip=%s asset=%s ua=%r",
            room_id,
            get_client_ip(request),
            stream_path,
            request.headers.get("user-agent", ""),
        )

    # The manifest mutates while transcoding (segments are appended, then
    # #EXT-X-ENDLIST is written), so it must never be cached -- otherwise the
    # client keeps replaying a stale, endless live playlist. Segments are
    # immutable once written and safe to cache aggressively.
    if is_manifest:
        cache_control = "no-store, no-cache, must-revalidate"
    else:
        cache_control = "public, max-age=31536000, immutable"

    return FileResponse(
        path=asset_path,
        media_type=guess_video_media_type(asset_path),
        filename=asset_path.name,
        content_disposition_type="inline",
        headers={
            "Cache-Control": cache_control,
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
        ip=get_client_ip(websocket),
        user_agent=websocket.headers.get("user-agent", ""),
        joined_at=now_ms(),
    )
    CLIENTS[websocket] = client_meta
    room.clients.add(websocket)

    logger.info(
        "viewer joined room=%s ip=%s nickname=%s client=%s ua=%r",
        room_id,
        client_meta.ip,
        client_meta.nickname,
        client_meta.client_id,
        client_meta.user_agent,
    )

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

                # Guard against a freshly-joined client that fires play/pause at
                # ~position 0 before it has synced to the room (e.g. the iOS
                # inline player starting at 0). Honoring it would drag everyone
                # back to the start, so ignore it during a short join grace
                # window while the room is meaningfully ahead, and re-push the
                # authoritative state so the stale client corrects itself.
                JOIN_SYNC_GRACE_MS = 4000
                if (
                    message_type in {"play", "pause"}
                    and position < 1.0
                    and effective_position(room) > 1.0
                    and now_ms() - client_meta.joined_at < JOIN_SYNC_GRACE_MS
                ):
                    logger.info(
                        "ignore stale op=%s room=%s client=%s position=%.3f "
                        "(room at %.3f within join grace)",
                        message_type,
                        room_id,
                        client_meta.client_id,
                        position,
                        effective_position(room),
                    )
                    await websocket.send_json(serialize_state(room))
                    continue

                # seek fires repeatedly while scrubbing, so keep it at DEBUG;
                # play/pause are the meaningful, low-frequency actions.
                op_log = logger.info if message_type in {"play", "pause"} else logger.debug
                op_log(
                    "op=%s room=%s ip=%s client=%s position=%.3f",
                    message_type,
                    room_id,
                    client_meta.ip,
                    client_meta.client_id,
                    position,
                )

                if message_type == "play":
                    await update_room_state(room, True, position)
                elif message_type == "pause":
                    await update_room_state(room, False, position)
                else:
                    await update_room_state(room, None, position)

            elif message_type == "client_log":
                # Playback diagnostics reported by the browser (e.g. the video
                # element errored). Logged with the viewer's IP so a "can't play"
                # report can be traced from the server side.
                logger.warning(
                    "client_log room=%s ip=%s client=%s event=%s detail=%r ua=%r",
                    room_id,
                    client_meta.ip,
                    client_meta.client_id,
                    str(payload.get("event", "")),
                    str(payload.get("detail", ""))[:500],
                    client_meta.user_agent,
                )

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
        logger.info(
            "viewer left room=%s ip=%s client=%s",
            room_id,
            client_meta.ip,
            client_meta.client_id,
        )
        await broadcast_viewers(room)
