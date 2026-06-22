(() => {
    const context = window.__ROOM_CONTEXT__;
    if (!context) {
        return;
    }

    const elements = {
        video: document.getElementById("video-player"),
        danmakuLayer: document.getElementById("danmaku-layer"),
        joinOverlay: document.getElementById("join-overlay"),
        joinButton: document.getElementById("join-playback-btn"),
        playPause: document.getElementById("play-pause-btn"),
        progress: document.getElementById("progress-range"),
        timeLabel: document.getElementById("time-label"),
        viewerCount: document.getElementById("viewer-count"),
        nicknameLabel: document.getElementById("nickname-label"),
        copyLink: document.getElementById("copy-link-btn"),
        shareLink: document.getElementById("share-link-input"),
        uploadInput: document.getElementById("upload-input"),
        uploadStatus: document.getElementById("upload-status"),
        roomStatus: document.getElementById("room-status"),
        danmakuInput: document.getElementById("danmaku-input"),
        sendDanmaku: document.getElementById("send-danmaku-btn"),
    };

    const state = {
        ws: null,
        clientId: null,
        nickname: null,
        currentVideoUrl: null,
        hasUserGesture: false,
        pendingRemotePlay: false,
        pendingState: null,
        applyingRemote: false,
        suppressUntil: 0,
        suppressTimer: null,
        isVideoReady: false,
        isScrubbing: false,
        lastDanmakuScanTime: 0,
        danmakuHistory: [],
        renderedDanmakuIds: new Set(),
        optimisticDanmaku: new Map(),
        nextLane: 0,
    };

    function setRoomStatus(text) {
        elements.roomStatus.textContent = text;
    }

    function setUploadStatus(text) {
        elements.uploadStatus.textContent = text;
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

    function updateTimeLabel(currentTime = 0, duration = 0) {
        elements.timeLabel.textContent = `${formatTime(currentTime)} / ${formatTime(duration)}`;
    }

    function setViewerCount(count) {
        elements.viewerCount.textContent = `在线 ${count}`;
    }

    function setJoinOverlayVisible(visible) {
        elements.joinOverlay.classList.toggle("visible", visible);
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

    async function ensureVideoSource(videoUrl) {
        if (!videoUrl || state.currentVideoUrl === videoUrl) {
            return;
        }
        state.currentVideoUrl = videoUrl;
        state.isVideoReady = true;
        updateControlsAvailability();
        elements.video.src = videoUrl;
        elements.video.load();
        resetDanmakuWindow(0);
        setRoomStatus("视频已就绪，可以开始同步播放");
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
            await ensureVideoSource(message.video_url);
        }

        if (elements.video.readyState < 1) {
            return;
        }

        const targetTime = message.is_playing
            ? message.position + (Date.now() - message.server_time) / 1000
            : message.position;
        const normalizedTime = Math.max(0, targetTime);

        if (Math.abs((elements.video.currentTime || 0) - normalizedTime) > 0.5) {
            suppressLocalEvents();
            elements.video.currentTime = normalizedTime;
            resetDanmakuWindow(normalizedTime);
        }

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
        const signature = signatureForDanmaku(message.text, message.video_time);

        if (
            message.sender_id === state.clientId &&
            state.optimisticDanmaku.has(signature)
        ) {
            state.optimisticDanmaku.delete(signature);
            return;
        }

        if (Math.abs((elements.video.currentTime || 0) - message.video_time) <= 0.3) {
            state.renderedDanmakuIds.add(message.id);
            spawnDanmaku(message);
        }
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
                setRoomStatus("视频上传完成，正在同步加载");
                if (payload.video_url) {
                    await ensureVideoSource(payload.video_url);
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

    async function uploadSelectedFile(file) {
        if (!file) {
            return;
        }

        setUploadStatus(`上传中：${file.name}`);
        try {
            const response = await fetch(`/room/${encodeURIComponent(context.roomId)}/upload`, {
                method: "POST",
                headers: {
                    "Content-Type": "application/octet-stream",
                    "X-Filename": encodeURIComponent(file.name),
                },
                body: file,
            });

            if (!response.ok) {
                const errorPayload = await response.json().catch(() => ({}));
                throw new Error(errorPayload.detail || "上传失败");
            }

            setUploadStatus(`上传完成：${file.name}`);
        } catch (error) {
            setUploadStatus(error.message || "上传失败");
        } finally {
            elements.uploadInput.value = "";
        }
    }

    function sendDanmaku() {
        const text = elements.danmakuInput.value.trim();
        if (!text || !state.clientId || !state.isVideoReady) {
            return;
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
        elements.danmakuInput.value = "";
    }

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

    elements.video.addEventListener("durationchange", refreshTransportUi);

    elements.video.addEventListener("play", () => {
        refreshTransportUi();
        if (localEventsBlocked()) {
            return;
        }
        sendMessage({
            type: "play",
            position: elements.video.currentTime || 0,
        });
    });

    elements.video.addEventListener("pause", () => {
        refreshTransportUi();
        if (localEventsBlocked()) {
            return;
        }
        sendMessage({
            type: "pause",
            position: elements.video.currentTime || 0,
        });
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
    elements.danmakuInput.addEventListener("keydown", (event) => {
        if (event.key === "Enter") {
            event.preventDefault();
            sendDanmaku();
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
    setRoomStatus("正在连接房间...");
    loadHistoryDanmaku().catch(() => {});
    connectWebSocket();
    animationLoop();
})();
