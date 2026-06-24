(() => {
    const context = window.__ROOM_CONTEXT__;
    if (!context) {
        return;
    }

    const elements = {
        documentElement: document.documentElement,
        body: document.body,
        playerShell: document.getElementById("player-shell"),
        video: document.getElementById("video-player"),
        danmakuLayer: document.getElementById("danmaku-layer"),
        joinOverlay: document.getElementById("join-overlay"),
        joinButton: document.getElementById("join-playback-btn"),
        playPause: document.getElementById("play-pause-btn"),
        fullscreenToggle: document.getElementById("fullscreen-toggle-btn"),
        progress: document.getElementById("progress-range"),
        timeLabel: document.getElementById("time-label"),
        viewerCount: document.getElementById("viewer-count"),
        nicknameLabel: document.getElementById("nickname-label"),
        copyLink: document.getElementById("copy-link-btn"),
        shareLink: document.getElementById("share-link-input"),
        uploadTrigger: document.getElementById("upload-trigger-btn"),
        uploadInput: document.getElementById("upload-input"),
        uploadStatus: document.getElementById("upload-status"),
        roomStatus: document.getElementById("room-status"),
        danmakuInput: document.getElementById("danmaku-input"),
        sendDanmaku: document.getElementById("send-danmaku-btn"),
        fullscreenDanmakuComposer: document.getElementById("fullscreen-danmaku-composer"),
        fullscreenDanmakuTrigger: document.getElementById("fullscreen-danmaku-trigger-btn"),
        fullscreenDanmakuInput: document.getElementById("fullscreen-danmaku-input"),
        fullscreenSendDanmaku: document.getElementById("fullscreen-send-danmaku-btn"),
    };

    const state = {
        ws: null,
        hls: null,
        clientId: null,
        nickname: null,
        currentVideoUrl: null,
        currentVideoType: null,
        currentFallbackUrl: null,
        currentSourceKey: null,
        hasUserGesture: false,
        pendingRemotePlay: false,
        pendingState: null,
        applyingRemote: false,
        suppressUntil: 0,
        suppressTimer: null,
        hasSyncedRoomState: false,
        isVideoReady: false,
        isScrubbing: false,
        isPseudoFullscreen: false,
        isFullscreenComposerActive: false,
        fullscreenComposerTimer: null,
        lastDanmakuScanTime: 0,
        danmakuHistory: [],
        renderedDanmakuIds: new Set(),
        optimisticDanmaku: new Map(),
        nextLane: 0,
        danmakuDraft: "",
        uploadInFlight: false,
        isTouchDevice: detectTouchDevice(),
    };

    const UPLOAD_RETRY_LIMIT = 4;
    const UPLOAD_RETRY_BASE_DELAY = 1200;

    function detectTouchDevice() {
        return (
            window.matchMedia("(pointer: coarse)").matches ||
            navigator.maxTouchPoints > 0 ||
            "ontouchstart" in window ||
            /android|iphone|ipad|ipod|mobile/i.test(navigator.userAgent)
        );
    }

    function setRoomStatus(text) {
        elements.roomStatus.textContent = text;
    }

    function setUploadStatus(text) {
        elements.uploadStatus.textContent = text;
    }

    function setUploadEnabled(enabled) {
        elements.uploadInput.disabled = !enabled;
        elements.uploadTrigger.setAttribute("aria-disabled", String(!enabled));
    }

    function formatTime(totalSeconds) {
        if (!Number.isFinite(totalSeconds) || totalSeconds < 0) {
            return "00:00";
        }

        const seconds = Math.floor(totalSeconds);
        const hours = Math.floor(seconds / 3600);
        const minutes = Math.floor((seconds % 3600) / 60);
        const remainder = seconds % 60;
        if (hours > 0) {
            return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
        }
        return `${String(minutes).padStart(2, "0")}:${String(remainder).padStart(2, "0")}`;
    }

    function formatBytes(totalBytes) {
        if (!Number.isFinite(totalBytes) || totalBytes < 0) {
            return "0 B";
        }

        const units = ["B", "KB", "MB", "GB", "TB"];
        let value = totalBytes;
        let unitIndex = 0;
        while (value >= 1024 && unitIndex < units.length - 1) {
            value /= 1024;
            unitIndex += 1;
        }
        const digits = value >= 100 || unitIndex === 0 ? 0 : 1;
        return `${value.toFixed(digits)} ${units[unitIndex]}`;
    }

    function uploadProgressLabel(fileName, uploadedBytes, totalBytes, resumed = false) {
        const safeTotal = Math.max(totalBytes, 1);
        const percent = Math.min(100, Math.floor((uploadedBytes / safeTotal) * 100));
        const prefix = resumed ? "继续上传" : "上传中";
        return `${prefix} ${percent}%：${fileName} (${formatBytes(uploadedBytes)} / ${formatBytes(totalBytes)})`;
    }

    function sleep(milliseconds) {
        return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
    }

    function shouldRetryUpload(statusCode) {
        return statusCode === 408 || statusCode === 409 || statusCode === 425 ||
            statusCode === 429 || statusCode >= 500;
    }

    async function readJsonSafely(response) {
        return response.json().catch(() => ({}));
    }

    function updateTimeLabel(currentTime = 0, duration = 0) {
        elements.timeLabel.textContent = `${formatTime(currentTime)} / ${formatTime(duration)}`;
    }

    function setViewerCount(count) {
        elements.viewerCount.textContent = `在线 ${count}`;
    }

    function setJoinOverlayVisible(visible) {
        elements.joinOverlay.classList.toggle("visible", visible);
    }

    function describeStreamStatus(streamStatus) {
        if (streamStatus === "processing") {
            return "正在后台转码为更兼容的格式，当前先用原视频播放";
        }
        if (streamStatus === "failed") {
            return "转码失败，当前使用原视频直链播放";
        }
        return "视频已就绪，可以开始同步播放";
    }

    function canPlayNativeHls() {
        return elements.video.canPlayType("application/vnd.apple.mpegurl") !== "";
    }

    function destroyHlsInstance() {
        if (!state.hls) {
            return;
        }

        state.hls.destroy();
        state.hls = null;
    }

    function suppressLocalEvents(milliseconds = 700) {
        state.applyingRemote = true;
        state.suppressUntil = Math.max(state.suppressUntil, Date.now() + milliseconds);
        window.clearTimeout(state.suppressTimer);
        state.suppressTimer = window.setTimeout(() => {
            if (Date.now() >= state.suppressUntil) {
                state.applyingRemote = false;
            }
        }, milliseconds + 20);
    }

    function localEventsBlocked() {
        return state.applyingRemote || Date.now() < state.suppressUntil;
    }

    function resetDanmakuWindow(timePoint) {
        state.renderedDanmakuIds.clear();
        state.lastDanmakuScanTime = Math.max(timePoint - 0.35, 0);
    }

    function updateControlsAvailability() {
        const enabled = state.isVideoReady;
        elements.playPause.disabled = !enabled;
        elements.progress.disabled = !enabled;
    }

    function signatureForDanmaku(text, videoTime) {
        return `${text}|${Math.round(videoTime * 10)}`;
    }

    function sendMessage(payload) {
        if (!state.ws || state.ws.readyState !== WebSocket.OPEN) {
            return;
        }
        state.ws.send(JSON.stringify(payload));
    }

    function reportClientLog(event, detail) {
        sendMessage({
            type: "client_log",
            event,
            detail: typeof detail === "string" ? detail : JSON.stringify(detail),
        });
    }

    function pruneOptimisticDanmaku() {
        const now = Date.now();
        for (const [key, expiresAt] of state.optimisticDanmaku.entries()) {
            if (expiresAt <= now) {
                state.optimisticDanmaku.delete(key);
            }
        }
    }

    function laneCount() {
        return window.innerWidth <= 720 ? 4 : 6;
    }

    function spawnDanmaku(item) {
        const node = document.createElement("span");
        const lane = state.nextLane % laneCount();
        const lanes = laneCount();
        const step = lanes === 1 ? 0 : 70 / (lanes - 1);
        const top = 10 + lane * step;
        state.nextLane += 1;

        node.className = `danmaku-item ${item.sender_id === state.clientId ? "danmaku-self" : "danmaku-other"}`;
        node.style.top = `${top}%`;
        node.style.animationDuration = `${Math.min(12, Math.max(7, 6 + item.text.length * 0.18))}s`;
        node.textContent = item.text;
        elements.danmakuLayer.appendChild(node);
        node.addEventListener("animationend", () => node.remove());
    }

    function addDanmakuToHistory(item) {
        state.danmakuHistory.push(item);
        state.danmakuHistory.sort((left, right) => {
            if (left.video_time !== right.video_time) {
                return left.video_time - right.video_time;
            }
            return left.id - right.id;
        });
    }

    function maybeEmitDanmaku(currentTime) {
        if (!state.danmakuHistory.length) {
            state.lastDanmakuScanTime = currentTime;
            return;
        }

        if (Math.abs(currentTime - state.lastDanmakuScanTime) > 1.2) {
            resetDanmakuWindow(currentTime);
            return;
        }

        const from = Math.min(state.lastDanmakuScanTime, currentTime) - 0.3;
        const to = Math.max(state.lastDanmakuScanTime, currentTime) + 0.3;

        for (const item of state.danmakuHistory) {
            if (item.video_time < from || item.video_time > to) {
                continue;
            }
            if (state.renderedDanmakuIds.has(item.id)) {
                continue;
            }
            state.renderedDanmakuIds.add(item.id);
            spawnDanmaku(item);
        }

        state.lastDanmakuScanTime = currentTime;
    }

    function refreshTransportUi() {
        const duration = Number.isFinite(elements.video.duration) ? elements.video.duration : 0;
        if (!state.isScrubbing) {
            elements.progress.max = duration || 0;
            elements.progress.value = Math.min(elements.video.currentTime || 0, duration || 0);
        }
        updateTimeLabel(elements.video.currentTime || 0, duration);
        elements.playPause.textContent = elements.video.paused ? "播放" : "暂停";
    }

    function describeVideoError() {
        const mediaError = elements.video.error;
        if (!mediaError) {
            return "视频加载失败";
        }

        if (mediaError.code === mediaError.MEDIA_ERR_ABORTED) {
            return "视频加载已取消";
        }
        if (mediaError.code === mediaError.MEDIA_ERR_NETWORK) {
            return "视频下载中断，请重试";
        }
        if (mediaError.code === mediaError.MEDIA_ERR_DECODE) {
            return "视频解码失败，流媒体编码可能不兼容";
        }
        if (mediaError.code === mediaError.MEDIA_ERR_SRC_NOT_SUPPORTED) {
            return "当前设备不支持此播放源，浏览器可能不支持 HLS 或回退视频格式";
        }
        return "视频加载失败";
    }

    function syncDanmakuDraft(value, source = null) {
        state.danmakuDraft = value;
        if (source !== elements.danmakuInput) {
            elements.danmakuInput.value = value;
        }
        if (source !== elements.fullscreenDanmakuInput) {
            elements.fullscreenDanmakuInput.value = value;
        }
    }

    function clearFullscreenComposerTimer() {
        window.clearTimeout(state.fullscreenComposerTimer);
        state.fullscreenComposerTimer = null;
    }

    function isNativeFullscreenActive() {
        return (
            document.fullscreenElement === elements.playerShell ||
            document.webkitFullscreenElement === elements.playerShell
        );
    }

    function isFullscreenActive() {
        return state.isPseudoFullscreen || isNativeFullscreenActive();
    }

    function scheduleFullscreenComposerIdle() {
        if (!state.isTouchDevice || !isFullscreenActive()) {
            return;
        }
        clearFullscreenComposerTimer();
        state.fullscreenComposerTimer = window.setTimeout(() => {
            if (document.activeElement !== elements.fullscreenDanmakuInput) {
                setFullscreenComposerActive(false);
            }
        }, 4000);
    }

    function setFullscreenComposerActive(active, options = {}) {
        const {restartTimer = false} = options;
        state.isFullscreenComposerActive = active;
        elements.fullscreenDanmakuComposer.classList.toggle("is-active", active);

        if (!active) {
            clearFullscreenComposerTimer();
            return;
        }

        if (state.isTouchDevice && restartTimer) {
            scheduleFullscreenComposerIdle();
        } else {
            clearFullscreenComposerTimer();
        }
    }

    function updateFullscreenUi() {
        const fullscreen = isFullscreenActive();
        elements.documentElement.classList.toggle("is-fullscreen", fullscreen);
        elements.body.classList.toggle("is-fullscreen", fullscreen);
        elements.playerShell.classList.toggle("is-fullscreen", fullscreen);
        elements.playerShell.classList.toggle("pseudo-fullscreen", state.isPseudoFullscreen);
        elements.fullscreenDanmakuComposer.setAttribute("aria-hidden", String(!fullscreen));
        elements.fullscreenToggle.textContent = fullscreen ? "退出全屏" : "全屏";
        elements.fullscreenToggle.setAttribute("aria-pressed", String(fullscreen));

        if (!fullscreen) {
            setFullscreenComposerActive(false);
            if (document.activeElement === elements.fullscreenDanmakuInput) {
                elements.fullscreenDanmakuInput.blur();
            }
        } else {
            syncDanmakuDraft(state.danmakuDraft);
            setFullscreenComposerActive(false);
        }
    }

    async function requestNativeFullscreen() {
        const requestFullscreen =
            elements.playerShell.requestFullscreen ||
            elements.playerShell.webkitRequestFullscreen;
        if (!requestFullscreen) {
            throw new Error("Fullscreen API unavailable");
        }

        const result = requestFullscreen.call(elements.playerShell);
        if (result && typeof result.then === "function") {
            await result;
        }
    }

    async function exitNativeFullscreen() {
        const exitFullscreen = document.exitFullscreen || document.webkitExitFullscreen;
        if (!exitFullscreen) {
            return;
        }

        const result = exitFullscreen.call(document);
        if (result && typeof result.then === "function") {
            await result;
        }
    }

    function enablePseudoFullscreen() {
        state.isPseudoFullscreen = true;
        updateFullscreenUi();
    }

    function disablePseudoFullscreen() {
        state.isPseudoFullscreen = false;
        updateFullscreenUi();
    }

    async function enterFullscreenMode() {
        if (state.isTouchDevice) {
            enablePseudoFullscreen();
            return;
        }

        try {
            await requestNativeFullscreen();
        } catch (error) {
            enablePseudoFullscreen();
        }
    }

    async function exitFullscreenMode() {
        if (state.isPseudoFullscreen) {
            disablePseudoFullscreen();
            return;
        }

        if (isNativeFullscreenActive()) {
            await exitNativeFullscreen();
        } else {
            disablePseudoFullscreen();
        }
    }

    async function toggleFullscreenMode() {
        if (isFullscreenActive()) {
            await exitFullscreenMode();
            return;
        }
        await enterFullscreenMode();
    }

    function applyDirectVideoSource(videoUrl, videoType, fallbackUrl) {
        destroyHlsInstance();
        state.currentVideoUrl = videoUrl;
        state.currentVideoType = videoType;
        state.currentFallbackUrl = fallbackUrl;
        state.currentSourceKey = `${videoType || ""}|${videoUrl}`;
        elements.video.src = videoUrl;
        elements.video.load();
    }

    function fallbackToDirectVideo(fallbackUrl) {
        if (!fallbackUrl) {
            return false;
        }

        applyDirectVideoSource(fallbackUrl, "video/mp4", null);
        setRoomStatus("HLS 加载失败，已回退到 MP4 直链播放");
        return true;
    }

    function bindHlsSource(videoUrl, fallbackUrl) {
        // Prefer hls.js (MSE) wherever it is supported. Some Chromium builds
        // report a non-empty canPlayType() for HLS ("maybe") yet their native
        // demuxer cannot actually parse a playlist (DEMUXER_ERROR_COULD_NOT_PARSE),
        // so native HLS is only trustworthy when hls.js is unavailable -- i.e.
        // iOS Safari, which has no MSE for <video> but does play HLS natively.
        if (!window.Hls || !window.Hls.isSupported()) {
            if (canPlayNativeHls()) {
                applyDirectVideoSource(videoUrl, "application/vnd.apple.mpegurl", fallbackUrl);
                return true;
            }
            return fallbackToDirectVideo(fallbackUrl);
        }

        destroyHlsInstance();
        state.currentVideoUrl = videoUrl;
        state.currentVideoType = "application/vnd.apple.mpegurl";
        state.currentFallbackUrl = fallbackUrl;
        state.currentSourceKey = `application/vnd.apple.mpegurl|${videoUrl}`;

        const hls = new window.Hls({
            enableWorker: true,
        });
        state.hls = hls;
        hls.attachMedia(elements.video);
        hls.on(window.Hls.Events.MEDIA_ATTACHED, () => {
            hls.loadSource(videoUrl);
        });
        hls.on(window.Hls.Events.ERROR, (_event, data) => {
            if (!data || !data.fatal) {
                return;
            }

            reportClientLog("hls_error", {
                type: data.type,
                details: data.details,
                videoUrl,
                fallbackUrl: fallbackUrl || null,
                response: data.response ? data.response.code : null,
            });

            destroyHlsInstance();
            if (!fallbackToDirectVideo(fallbackUrl)) {
                state.isVideoReady = false;
                updateControlsAvailability();
                setRoomStatus("HLS 加载失败，且当前浏览器没有可用的回退播放方式");
            }
        });
        return true;
    }

    async function ensureVideoSource(source) {
        const videoUrl = source && source.video_url;
        const videoType = source && source.video_type;
        const fallbackUrl = source && source.fallback_video_url;
        const streamStatus = source && source.stream_status;
        // The dedup key intentionally excludes the fallback URL: a fallback whose
        // ?v= version changed (e.g. after the MP4 faststart remux) must not force
        // a reload of the source the viewer is already watching.
        const sourceKey = `${videoType || ""}|${videoUrl || ""}`;

        // Keep the latest fallback available for the <video> error handler even
        // when we don't rebind.
        state.currentFallbackUrl = fallbackUrl;

        if (!videoUrl || state.currentSourceKey === sourceKey) {
            return;
        }

        state.isVideoReady = true;
        updateControlsAvailability();

        if (videoType === "application/vnd.apple.mpegurl") {
            if (!bindHlsSource(videoUrl, fallbackUrl)) {
                state.isVideoReady = false;
                updateControlsAvailability();
                setRoomStatus("当前浏览器不支持 HLS 播放");
                return;
            }
        } else {
            applyDirectVideoSource(videoUrl, videoType, fallbackUrl);
        }

        resetDanmakuWindow(0);
        setRoomStatus(describeStreamStatus(streamStatus));
    }

    // The room's authoritative playback position right now, derived from the
    // latest state message (advancing it by elapsed wall-clock time while the
    // room is playing). Returns null when no room state is known yet.
    function roomPositionFromState(message) {
        if (!message || !message.video_ready) {
            return null;
        }
        const base = message.is_playing
            ? message.position + (Date.now() - message.server_time) / 1000
            : message.position;
        return Math.max(0, base);
    }

    async function applyRemoteState(message) {
        state.pendingState = message;

        if (!message.video_ready) {
            state.isVideoReady = false;
            updateControlsAvailability();
            setRoomStatus("等待视频上传");
            return;
        }

        if (message.video_url) {
            await ensureVideoSource(message);
        }

        if (elements.video.readyState < 1) {
            return;
        }

        const normalizedTime = roomPositionFromState(message);

        if (Math.abs((elements.video.currentTime || 0) - normalizedTime) > 0.5) {
            suppressLocalEvents();
            elements.video.currentTime = normalizedTime;
            resetDanmakuWindow(normalizedTime);
        }

        // We've now aligned the local <video> to the room's position, so local
        // play/pause events may be broadcast as authoritative from here on.
        state.hasSyncedRoomState = true;

        if (message.is_playing) {
            setRoomStatus("房间正在播放");
            if (!state.hasUserGesture) {
                state.pendingRemotePlay = true;
                setJoinOverlayVisible(true);
                refreshTransportUi();
                return;
            }

            try {
                suppressLocalEvents();
                await elements.video.play();
                state.pendingRemotePlay = false;
            } catch (error) {
                state.pendingRemotePlay = true;
                setJoinOverlayVisible(true);
                setRoomStatus("浏览器需要点击一次“加入播放”后才能自动同步播放");
            }
        } else {
            state.pendingRemotePlay = false;
            setRoomStatus("房间已暂停");
            if (!elements.video.paused) {
                suppressLocalEvents();
                elements.video.pause();
            }
        }

        refreshTransportUi();
    }

    async function handleDanmaku(message) {
        addDanmakuToHistory(message);
        // 标记为已渲染，避免随后的历史扫描 (maybeEmitDanmaku) 把这条直播弹幕再播一次
        state.renderedDanmakuIds.add(message.id);

        // 自己发的弹幕已经在发送时乐观渲染过，命中乐观记录则跳过，避免重复显示
        if (message.sender_id === state.clientId) {
            const signature = signatureForDanmaku(message.text, message.video_time);
            if (state.optimisticDanmaku.has(signature)) {
                state.optimisticDanmaku.delete(signature);
                return;
            }
        }

        // 通过 WebSocket 收到的都是实时弹幕，立即展示，不受双方播放进度是否对齐的影响
        spawnDanmaku(message);
    }

    async function loadHistoryDanmaku() {
        const response = await fetch(`/room/${encodeURIComponent(context.roomId)}/danmaku`);
        if (!response.ok) {
            return;
        }
        const items = await response.json();
        state.danmakuHistory = items;
        if (elements.video.readyState >= 1) {
            resetDanmakuWindow(elements.video.currentTime || 0);
        }
    }

    function connectWebSocket() {
        const protocol = window.location.protocol === "https:" ? "wss" : "ws";
        const endpoint = `${protocol}://${window.location.host}/room/${encodeURIComponent(context.roomId)}/ws`;
        const socket = new WebSocket(endpoint);
        state.ws = socket;
        // A fresh connection hasn't aligned to the room position yet; block any
        // local play/pause from broadcasting a stale position until we re-sync.
        state.hasSyncedRoomState = false;

        socket.addEventListener("open", () => {
            setRoomStatus("WebSocket 已连接，等待房间状态");
        });

        socket.addEventListener("message", async (event) => {
            const payload = JSON.parse(event.data);
            if (payload.type === "welcome") {
                state.clientId = payload.client_id;
                state.nickname = payload.nickname;
                elements.nicknameLabel.textContent = `你的昵称：${payload.nickname}`;
                return;
            }

            if (payload.type === "state") {
                await applyRemoteState(payload);
                return;
            }

            if (payload.type === "viewers") {
                setViewerCount(payload.count);
                return;
            }

            if (payload.type === "video_ready") {
                if (payload.stream_status === "processing") {
                    setRoomStatus("视频上传完成，已可用原视频播放，正在后台转码兼容格式");
                } else if (payload.stream_status === "ready") {
                    setRoomStatus("转码完成，正在切换到兼容播放源");
                } else {
                    setRoomStatus("视频上传完成，正在同步加载");
                }
                if (payload.video_url) {
                    await ensureVideoSource(payload);
                }
                return;
            }

            if (payload.type === "danmaku") {
                await handleDanmaku(payload);
            }
        });

        socket.addEventListener("close", () => {
            setRoomStatus("连接断开，2 秒后重连");
            window.setTimeout(connectWebSocket, 2000);
        });
    }

    async function unlockPlayback() {
        state.hasUserGesture = true;
        setJoinOverlayVisible(false);
        if (state.pendingRemotePlay && state.pendingState) {
            await applyRemoteState(state.pendingState);
        }
    }

    async function initUploadSession(file) {
        const response = await fetch(`/room/${encodeURIComponent(context.roomId)}/upload`, {
            method: "POST",
            headers: {
                "Content-Type": "application/json",
            },
            body: JSON.stringify({
                filename: file.name,
                size: file.size,
                last_modified: file.lastModified || 0,
            }),
        });

        const payload = await readJsonSafely(response);
        if (!response.ok) {
            throw new Error(payload.detail || "初始化上传失败");
        }
        return payload;
    }

    async function sendUploadChunk(file, uploadId, offset, chunkSize) {
        const end = Math.min(offset + chunkSize, file.size);
        const blob = file.slice(offset, end);
        const response = await fetch(
            `/room/${encodeURIComponent(context.roomId)}/upload/${encodeURIComponent(uploadId)}/chunk`,
            {
                method: "POST",
                headers: {
                    "Content-Type": "application/octet-stream",
                    "X-Upload-Offset": String(offset),
                    "X-Upload-Chunk-Bytes": String(blob.size),
                },
                body: blob,
            }
        );
        const payload = await readJsonSafely(response);
        return {response, payload};
    }

    async function completeUploadSession(uploadId) {
        const response = await fetch(
            `/room/${encodeURIComponent(context.roomId)}/upload/${encodeURIComponent(uploadId)}/complete`,
            {method: "POST"}
        );
        const payload = await readJsonSafely(response);
        if (!response.ok) {
            throw new Error(payload.detail || "完成上传失败");
        }
        return payload;
    }

    async function uploadChunkWithRetry(file, session, offset) {
        let currentSession = session;
        let lastError = null;

        for (let attempt = 1; attempt <= UPLOAD_RETRY_LIMIT; attempt += 1) {
            try {
                const {response, payload} = await sendUploadChunk(
                    file,
                    currentSession.upload_id,
                    offset,
                    currentSession.chunk_size
                );

                if (response.ok) {
                    return {
                        session: currentSession,
                        uploadedBytes: payload.uploaded_bytes,
                    };
                }

                if (response.status === 409 && Number.isFinite(payload.uploaded_bytes)) {
                    return {
                        session: currentSession,
                        uploadedBytes: payload.uploaded_bytes,
                    };
                }

                if (response.status === 404) {
                    currentSession = await initUploadSession(file);
                    continue;
                }

                const error = new Error(payload.detail || "上传分片失败");
                if (!shouldRetryUpload(response.status)) {
                    error.nonRetriable = true;
                    throw error;
                }
                lastError = error;
            } catch (error) {
                if (error && error.nonRetriable) {
                    throw error;
                }
                lastError = error;
            }

            if (attempt >= UPLOAD_RETRY_LIMIT) {
                break;
            }

            setUploadStatus(`${uploadProgressLabel(file.name, offset, file.size, offset > 0)}，正在重试`);
            await sleep(UPLOAD_RETRY_BASE_DELAY * attempt);

            try {
                currentSession = await initUploadSession(file);
            } catch (error) {
                lastError = error;
            }
        }

        throw lastError || new Error("上传失败");
    }

    async function uploadSelectedFile(file) {
        if (!file || state.uploadInFlight) {
            return;
        }

        state.uploadInFlight = true;
        setUploadEnabled(false);

        try {
            let session = await initUploadSession(file);
            let uploadedBytes = Number(session.uploaded_bytes || 0);
            if (!Number.isFinite(uploadedBytes) || uploadedBytes < 0 || uploadedBytes > file.size) {
                throw new Error("服务器返回了无效的上传进度");
            }

            setUploadStatus(uploadProgressLabel(file.name, uploadedBytes, file.size, uploadedBytes > 0));

            while (uploadedBytes < file.size) {
                const outcome = await uploadChunkWithRetry(file, session, uploadedBytes);
                session = outcome.session;

                if (!Number.isFinite(outcome.uploadedBytes) || outcome.uploadedBytes < uploadedBytes) {
                    throw new Error("上传进度回退，已中止");
                }

                uploadedBytes = Math.min(file.size, outcome.uploadedBytes);
                setUploadStatus(uploadProgressLabel(file.name, uploadedBytes, file.size, uploadedBytes > 0));
            }

            setUploadStatus(`正在提交：${file.name}`);
            const completion = await completeUploadSession(session.upload_id);
            if (completion.stream_status === "processing") {
                setUploadStatus(`上传完成，HLS 正在后台生成：${file.name}`);
            } else {
                setUploadStatus(`上传完成：${file.name} (${formatBytes(file.size)})`);
            }
        } catch (error) {
            setUploadStatus(error.message || "上传失败");
        } finally {
            state.uploadInFlight = false;
            setUploadEnabled(true);
            elements.uploadInput.value = "";
        }
    }

    function sendDanmaku() {
        const text = state.danmakuDraft.trim();
        if (!text || !state.clientId || !state.isVideoReady) {
            return false;
        }

        const videoTime = Number(elements.video.currentTime || 0);
        const signature = signatureForDanmaku(text, videoTime);
        state.optimisticDanmaku.set(signature, Date.now() + 3000);
        spawnDanmaku({
            text,
            sender_id: state.clientId,
        });
        sendMessage({
            type: "danmaku",
            text,
            video_time: videoTime,
        });
        syncDanmakuDraft("");
        if (document.activeElement === elements.fullscreenDanmakuInput) {
            elements.fullscreenDanmakuInput.blur();
        }
        setFullscreenComposerActive(false);
        return true;
    }

    function handleDanmakuInputSync(event) {
        syncDanmakuDraft(event.target.value, event.target);
        if (event.target === elements.fullscreenDanmakuInput) {
            setFullscreenComposerActive(true, {restartTimer: state.isTouchDevice});
        }
    }

    function handleDanmakuInputKeydown(event) {
        event.stopPropagation();

        if (event.key === "Enter") {
            event.preventDefault();
            sendDanmaku();
            return;
        }

        if (event.key === "Escape") {
            event.preventDefault();
            event.target.blur();
            if (event.target === elements.fullscreenDanmakuInput) {
                setFullscreenComposerActive(false);
            }
        }
    }

    function bindDanmakuInput(input) {
        input.addEventListener("input", handleDanmakuInputSync);
        input.addEventListener("keydown", handleDanmakuInputKeydown);
        input.addEventListener("keypress", (event) => event.stopPropagation());
        input.addEventListener("keyup", (event) => event.stopPropagation());
    }

    bindDanmakuInput(elements.danmakuInput);
    bindDanmakuInput(elements.fullscreenDanmakuInput);

    elements.fullscreenDanmakuInput.addEventListener("focus", () => {
        setFullscreenComposerActive(true);
    });

    elements.fullscreenDanmakuInput.addEventListener("blur", () => {
        window.setTimeout(() => {
            if (document.activeElement !== elements.fullscreenDanmakuInput) {
                setFullscreenComposerActive(false);
            }
        }, 0);
    });

    elements.fullscreenDanmakuComposer.addEventListener("mouseenter", () => {
        if (!state.isTouchDevice && isFullscreenActive()) {
            setFullscreenComposerActive(true);
        }
    });

    elements.fullscreenDanmakuComposer.addEventListener("mouseleave", () => {
        if (!state.isTouchDevice && document.activeElement !== elements.fullscreenDanmakuInput) {
            setFullscreenComposerActive(false);
        }
    });

    elements.fullscreenDanmakuComposer.addEventListener("pointerdown", () => {
        if (!isFullscreenActive()) {
            return;
        }
        setFullscreenComposerActive(true, {restartTimer: state.isTouchDevice});
    });

    elements.fullscreenDanmakuTrigger.addEventListener("click", () => {
        setFullscreenComposerActive(true, {restartTimer: state.isTouchDevice});
        elements.fullscreenDanmakuInput.focus();
    });

    elements.joinButton.addEventListener("click", unlockPlayback);

    elements.playPause.addEventListener("click", async () => {
        if (!state.hasUserGesture) {
            await unlockPlayback();
        }

        if (!state.isVideoReady) {
            return;
        }

        if (elements.video.paused) {
            await elements.video.play().catch(() => {
                setJoinOverlayVisible(true);
            });
        } else {
            elements.video.pause();
        }
    });

    elements.fullscreenToggle.addEventListener("click", async () => {
        state.hasUserGesture = true;
        await toggleFullscreenMode();
    });

    elements.video.addEventListener("click", async () => {
        if (!state.hasUserGesture) {
            await unlockPlayback();
        }
    });

    elements.progress.addEventListener("input", () => {
        state.isScrubbing = true;
        const duration = Number.isFinite(elements.video.duration) ? elements.video.duration : 0;
        updateTimeLabel(Number(elements.progress.value), duration);
    });

    elements.progress.addEventListener("change", async () => {
        state.isScrubbing = false;
        if (!state.isVideoReady) {
            return;
        }

        if (!state.hasUserGesture) {
            await unlockPlayback();
        }

        suppressLocalEvents();
        elements.video.currentTime = Number(elements.progress.value);
        resetDanmakuWindow(elements.video.currentTime || 0);
        refreshTransportUi();
        sendMessage({
            type: "seek",
            position: elements.video.currentTime || 0,
        });
    });

    elements.video.addEventListener("loadedmetadata", async () => {
        elements.progress.max = Number.isFinite(elements.video.duration) ? elements.video.duration : 0;
        refreshTransportUi();
        if (state.pendingState) {
            await applyRemoteState(state.pendingState);
        }
    });

    elements.video.addEventListener("error", () => {
        const mediaError = elements.video.error;
        reportClientLog("video_error", {
            message: describeVideoError(),
            code: mediaError ? mediaError.code : null,
            mediaMessage: mediaError ? mediaError.message : null,
            videoType: state.currentVideoType,
            videoUrl: state.currentVideoUrl,
            networkState: elements.video.networkState,
            readyState: elements.video.readyState,
        });

        // The active source failed. If it wasn't already the MP4 fallback and we
        // have one, switch to it. This covers native HLS playback (iOS Safari),
        // whose failures only surface as a <video> error, not an hls.js event.
        if (
            state.currentFallbackUrl &&
            state.currentVideoUrl !== state.currentFallbackUrl &&
            fallbackToDirectVideo(state.currentFallbackUrl)
        ) {
            return;
        }

        destroyHlsInstance();
        state.isVideoReady = false;
        updateControlsAvailability();
        setRoomStatus(describeVideoError());
    });

    elements.video.addEventListener("durationchange", refreshTransportUi);

    // Broadcast a local play/pause. On a freshly (re)connected client the user
    // can hit play before we've aligned to the room (notably the iOS inline
    // player, where the play can race the initial seek). Broadcasting the raw
    // currentTime there would be ~0 and clobber the room back to the start, so
    // first snap to the room's last known position and broadcast that instead.
    function broadcastTransport(type) {
        if (!state.hasSyncedRoomState) {
            const roomPosition = roomPositionFromState(state.pendingState);
            if (
                roomPosition !== null &&
                Math.abs((elements.video.currentTime || 0) - roomPosition) > 0.5
            ) {
                suppressLocalEvents();
                elements.video.currentTime = roomPosition;
                resetDanmakuWindow(roomPosition);
            }
            state.hasSyncedRoomState = true;
        }
        sendMessage({
            type,
            position: elements.video.currentTime || 0,
        });
    }

    elements.video.addEventListener("play", () => {
        refreshTransportUi();
        if (localEventsBlocked()) {
            return;
        }
        broadcastTransport("play");
    });

    elements.video.addEventListener("pause", () => {
        refreshTransportUi();
        if (localEventsBlocked()) {
            return;
        }
        broadcastTransport("pause");
    });

    elements.video.addEventListener("seeking", () => {
        if (localEventsBlocked()) {
            return;
        }
        resetDanmakuWindow(elements.video.currentTime || 0);
    });

    elements.uploadInput.addEventListener("change", async (event) => {
        const file = event.target.files && event.target.files[0];
        await uploadSelectedFile(file);
    });

    elements.copyLink.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(context.shareUrl);
            setRoomStatus("分享链接已复制");
        } catch (error) {
            elements.shareLink.select();
            document.execCommand("copy");
            setRoomStatus("分享链接已复制");
        }
    });

    elements.sendDanmaku.addEventListener("click", sendDanmaku);
    elements.fullscreenSendDanmaku.addEventListener("click", sendDanmaku);

    document.addEventListener("pointerdown", (event) => {
        if (!state.isTouchDevice || !isFullscreenActive()) {
            return;
        }

        if (elements.fullscreenDanmakuComposer.contains(event.target)) {
            return;
        }

        if (document.activeElement === elements.fullscreenDanmakuInput) {
            elements.fullscreenDanmakuInput.blur();
        }
        setFullscreenComposerActive(false);
    });

    document.addEventListener("fullscreenchange", () => {
        if (!state.isPseudoFullscreen) {
            updateFullscreenUi();
        }
    });

    document.addEventListener("webkitfullscreenchange", () => {
        if (!state.isPseudoFullscreen) {
            updateFullscreenUi();
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && state.isPseudoFullscreen) {
            event.preventDefault();
            exitFullscreenMode().catch(() => {});
        }
    });

    function animationLoop() {
        if (state.isVideoReady && elements.video.readyState >= 1) {
            refreshTransportUi();
            if (!elements.video.paused && !elements.video.ended) {
                maybeEmitDanmaku(elements.video.currentTime || 0);
            }
        }
        pruneOptimisticDanmaku();
        window.requestAnimationFrame(animationLoop);
    }

    updateControlsAvailability();
    updateTimeLabel(0, 0);
    syncDanmakuDraft("");
    updateFullscreenUi();
    setUploadEnabled(true);
    setRoomStatus("正在连接房间...");
    loadHistoryDanmaku().catch(() => {});
    connectWebSocket();
    animationLoop();
})();
