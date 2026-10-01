/* Keep the media element and its playback intent across server-side seeks. */
window.attachStreamingPlayer = function (player, options) {
    player.autoplay = true;
    player.controls = true;
    if (player.tagName !== 'VIDEO' || !window.MediaSource) {
        if (player.tagName === 'VIDEO') player.src = options.url;
        player.play().catch(() => {});
        return;
    }
    const MediaSourceClass = window.MediaSource;
    let controller, reader, objectUrl, mediaSource, buffer, bufferType;
    let positioning = false;
    let requestedPosition = 0;
    let duration = Number(options.duration);
    let isLive = false;
    let wantedPlaying = true;
    let pointerActive = false;
    let pointerWasPlaying = false;
    let pointerSeek = false;
    let seekTimer;
    const retryButton = options.startButton;
    retryButton.hidden = true;

    function contains(ranges, position) {
        for (let i = 0; i < ranges.length; i++) {
            if (position >= ranges.start(i) && position < ranges.end(i)) return true;
        }
        return false;
    }
    function stopReading() {
        if (controller) controller.abort();
        if (reader) reader.cancel().catch(() => {});
        reader = null;
    }
    function cleanup() {
        clearTimeout(seekTimer);
        stopReading();
        if (objectUrl) URL.revokeObjectURL(objectUrl);
        objectUrl = null;
    }
    function delay(signal, milliseconds = 200) {
        return new Promise(resolve => {
            const timer = setTimeout(done, milliseconds);
            function done() {
                clearTimeout(timer);
                signal.removeEventListener('abort', done);
                resolve();
            }
            signal.addEventListener('abort', done, {once: true});
            if (signal.aborted) done();
        });
    }
    function update(action, signal) {
        return new Promise((resolve, reject) => {
            function finish() { clear(); resolve(); }
            function fail() { clear(); reject(new Error('MP4の読み込みに失敗しました。')); }
            function abort() { clear(); reject(new DOMException('Aborted', 'AbortError')); }
            function clear() {
                buffer.removeEventListener('updateend', finish);
                buffer.removeEventListener('error', fail);
                signal.removeEventListener('abort', abort);
            }
            buffer.addEventListener('updateend', finish, {once: true});
            buffer.addEventListener('error', fail, {once: true});
            signal.addEventListener('abort', abort, {once: true});
            if (signal.aborted) { abort(); return; }
            try { action(); } catch (error) { clear(); reject(error); }
        });
    }
    function playIfWanted() {
        if (wantedPlaying) {
            // Autoplay restrictions leave the ordinary video controls available.
            player.play().catch(() => {});
        }
    }
    async function prepareBuffer(type, signal) {
        if (mediaSource && (player.error || mediaSource.readyState === "closed")) {
            if (objectUrl) URL.revokeObjectURL(objectUrl);
            mediaSource = buffer = bufferType = null;
        }
        if (!mediaSource) {
            mediaSource = new MediaSourceClass();
            const opened = new Promise((resolve, reject) => {
                function abort() { reject(new DOMException('Aborted', 'AbortError')); }
                signal.addEventListener('abort', abort, {once: true});
                mediaSource.addEventListener('sourceopen', () => {
                    signal.removeEventListener('abort', abort);
                    resolve();
                }, {once: true});
            });
            objectUrl = URL.createObjectURL(mediaSource);
            player.src = objectUrl;
            playIfWanted();
            await opened;
            if (signal.aborted) return;
        }
        if (!buffer) {
            buffer = mediaSource.addSourceBuffer(type);
            bufferType = type;
        } else {
            // Reuse the same SourceBuffer and src: load() during a seek loses play intent
            // and can require autoplay permission again after the user releases the bar.
            if (buffer.updating) await update(() => buffer.abort(), signal);
            if (buffer.buffered.length) {
                await update(() => buffer.remove(0, Math.max(Number.isFinite(duration) ? duration : 0, buffer.buffered.end(buffer.buffered.length - 1))), signal);
            }
            // A previous fetch can end between MP4 boxes even when updating is false.
            // Reset the segment parser before appending the replacement init segment.
            if (mediaSource.readyState === "open") buffer.abort();
            if (bufferType !== type) {
                buffer.changeType(type);
                bufferType = type;
            }
        }
        if (isLive) mediaSource.duration = Infinity;
        else if (duration > 0) mediaSource.duration = duration;
    }
    async function start(position = 0) {
        clearTimeout(seekTimer);
        position = Math.max(0, Number(position) || 0);
        if (duration > 0 && Number.isFinite(duration)) position = Math.min(position, Math.max(0, duration - 0.1));
        stopReading();
        positioning = true;
        requestedPosition = position;
        const current = new AbortController();
        controller = current;
        const signal = current.signal;
        options.error.textContent = '';
        retryButton.hidden = true;
        let failureMessage = '配信の読み込みに失敗しました。';
        try {
            const url = new URL(options.url, location.href);
            url.searchParams.set('start', position);
            let response;
            // A closed client may take a moment to release its server-side process.
            for (let attempt = 0; attempt < 20; attempt++) {
                response = await fetch(url, {cache: 'no-store', signal});
                if (response.status !== 429 || attempt === 19) break;
                await response.body.cancel();
                await delay(signal);
                if (signal.aborted) return;
            }
            if (signal.aborted) { await response.body.cancel(); return; }
            if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                failureMessage = data.error || '配信の開始に失敗しました。';
                throw new Error(failureMessage);
            }
            const streamDuration = Number(response.headers.get('X-Stream-Duration'));
            if (streamDuration > 0) duration = streamDuration;
            isLive = response.headers.get('X-Stream-Live') === '1';
            if (options.onMetadata) options.onMetadata();
            if (response.headers.get('X-Stream-Mode') !== 'remux') {
                await response.body.cancel();
                positioning = false;
                player.src = options.url;
                playIfWanted();
                return;
            }
            const codecs = response.headers.get('X-Stream-Codecs');
            const type = `video/mp4; codecs="${codecs}"`;
            if (!codecs || !MediaSourceClass.isTypeSupported(type)) {
                await response.body.cancel();
                failureMessage = 'この端末は選択されたMP4形式に対応していません。';
                throw new Error(failureMessage);
            }
            await prepareBuffer(type, signal);
            if (signal.aborted) return;
            if (!isLive && Math.abs(player.currentTime - position) > 0.1) player.currentTime = position;
            reader = response.body.getReader();
            const activeReader = reader;
            async function append(value) {
                for (let offset = 0; offset < value.byteLength && !signal.aborted; offset += 65536) {
                    while (!signal.aborted && buffer.buffered.length &&
                           buffer.buffered.end(buffer.buffered.length - 1) - Math.max(positioning ? position : 0, player.currentTime) > 30) {
                        await delay(signal);
                    }
                    if (signal.aborted) return;
                    if (buffer.buffered.length && player.currentTime > 20 &&
                        buffer.buffered.start(0) < player.currentTime - 15) {
                        await update(() => buffer.remove(0, player.currentTime - 10), signal);
                    }
                    await update(() => buffer.appendBuffer(value.subarray(offset, offset + 65536)), signal);
                    if (positioning && buffer.buffered.length && (isLive || contains(buffer.buffered, position))) {
                        const target = isLive ? buffer.buffered.start(0) : position;
                        if (Math.abs(player.currentTime - target) > 0.1) player.currentTime = target;
                        positioning = false;
                        playIfWanted();
                    }
                }
            }
            while (!signal.aborted) {
                const {value, done} = await activeReader.read();
                if (signal.aborted) return;
                if (done) {
                    if (mediaSource.readyState === 'open') {
                        mediaSource.endOfStream();
                        if (duration > 0 && mediaSource.duration < duration) mediaSource.duration = duration;
                    }
                    break;
                }
                await append(value);
            }
        } catch (error) {
            if (!signal.aborted) {
                current.abort();
                options.error.textContent = `${failureMessage} 再試行または保存型ダウンロードをご利用ください。`;
                retryButton.hidden = false;
            }
        }
    }
    player.addEventListener('play', () => { wantedPlaying = true; });
    player.addEventListener('pause', () => {
        if (!pointerSeek) wantedPlaying = false;
    });
    player.addEventListener('pointerdown', () => {
        pointerActive = true;
        pointerSeek = false;
        pointerWasPlaying = wantedPlaying;
    });
    function releasePointer() {
        if (pointerSeek && pointerWasPlaying) {
            wantedPlaying = true;
            playIfWanted();
        }
        pointerActive = pointerSeek = false;
    }
    window.addEventListener('pointerup', releasePointer);
    window.addEventListener('pointercancel', releasePointer);
    player.addEventListener('seeking', () => {
        if (pointerActive) {
            pointerSeek = true;
            wantedPlaying = pointerWasPlaying;
        }
        const position = player.currentTime;
        if (!isLive && buffer && ((positioning && Math.abs(position - requestedPosition) > 0.1) ||
            (!positioning && !contains(player.buffered, position)))) {
            stopReading();
            positioning = true;
            requestedPosition = position;
            clearTimeout(seekTimer);
            seekTimer = setTimeout(() => start(position), 200);
        }
    });
    retryButton.onclick = () => { wantedPlaying = true; start(requestedPosition); };
    player.addEventListener('error', () => {
        stopReading();
        retryButton.hidden = false;
    });
    window.addEventListener('pagehide', cleanup, {once: true});
    start();
};
