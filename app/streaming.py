"""On-demand YouTube streaming. Signed URLs never leave this module's cache."""
import atexit
from dataclasses import dataclass, field
from importlib.metadata import version, PackageNotFoundError
import re
import math
import subprocess
import threading
import time
from urllib.parse import urlparse, parse_qs

import requests
from flask import Response, request
from yt_dlp import YoutubeDL


class StreamError(Exception):
    def __init__(self, message, status=502):
        super().__init__(message)
        self.status = status


class SafeLogger:
    # yt-dlp messages may contain signed URLs, cookies or tokens. Do not forward them.
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass


def ytdlp_options(provider, bind_ip=None):
    options = {
        "noplaylist": True, "remote_components": {"ejs:github"},
        "extractor_args": {"youtubepot-bgutilhttp": {"base_url": [provider]}},
        "verbose": False, "debug_printtraffic": False, "logger": SafeLogger(),
        "socket_timeout": 20, "retries": 1, "extractor_retries": 1,
    }
    if bind_ip:
        options["source_address"] = bind_ip
    return options


def valid_source_url(url):
    if str(url).startswith("ytsearch:"):
        return True
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    return (parsed.scheme in ("https", "http") and
            (host in {"youtube.com", "youtu.be"} or host.endswith(".youtube.com"))
            and not parsed.username and not parsed.password and parsed.port in (None, 80, 443))


def normalize_source_url(url):
    """Unwrap Google's video-search link without fetching an arbitrary redirect."""
    parsed = urlparse(url or "")
    if parsed.scheme == "https" and parsed.hostname in {"google.com", "www.google.com"} and parsed.path == "/url":
        target = parse_qs(parsed.query).get("url", [""])[0]
        if valid_source_url(target):
            return target
    return url


def valid_media_url(url):
    parsed = urlparse(url or "")
    host = parsed.hostname or ""
    return (parsed.scheme == "https" and host.endswith(".googlevideo.com")
            and not parsed.username and not parsed.password and parsed.port in (None, 443))


def clean_headers(headers):
    return {str(k): str(v) for k, v in (headers or {}).items()
            if re.fullmatch(r"[A-Za-z0-9-]+", str(k))
            and k.lower() not in {"host", "range", "connection", "content-length", "transfer-encoding"}
            and "\r" not in str(v) and "\n" not in str(v)}


def select_formats(info, is_video=True, max_height=1080):
    live_status = info.get("live_status")
    if live_status == "is_upcoming":
        raise StreamError("ライブ配信はまだ始まっていません。")
    if info.get("is_live") or live_status == "is_live":
        formats = [f for f in info.get("formats", [])
                   if valid_media_url(f.get("url")) and f.get("protocol") in {"m3u8", "m3u8_native"}
                   and not f.get("has_drm")]
        audio = [f for f in formats if f.get("vcodec") == "none" and
                 (str(f.get("acodec", "")).startswith(("mp4a", "aac")) or f.get("format_id") in {"233", "234"})]
        if not audio:
            raise StreamError("対応するライブ音声がありません。")
        selected_audio = dict(max(audio, key=lambda f: (f.get("format_id") == "234", f.get("tbr") or f.get("abr") or 0)))
        selected_audio["acodec"] = "mp4a.40.2" if not str(selected_audio.get("acodec", "")).startswith(("mp4a", "aac")) else selected_audio["acodec"]
        if not is_video:
            raise StreamError("ライブの音声のみ再生には対応していません。")
        video = [f for f in formats if str(f.get("vcodec", "")).startswith(("avc1", "h264"))
                 and f.get("acodec") == "none" and 0 < (f.get("height") or 0) <= min(max_height, 1080)
                 and 0 < (f.get("width") or 0) <= 1920
                 and f.get("dynamic_range") in (None, "SDR")]
        if not video:
            raise StreamError("対応するライブ映像がありません。")
        return [max(video, key=lambda f: (f["height"], f.get("fps") or 0)), selected_audio]
    formats = [f for f in info.get("formats", [])
               if valid_media_url(f.get("url")) and f.get("protocol", "https") == "https"
               and not f.get("has_drm")]
    def aac(f):
        return str(f.get("acodec", "")).startswith(("mp4a", "aac"))
    def avc(f):
        return (str(f.get("vcodec", "")).startswith(("avc1", "h264"))
                and f.get("ext") == "mp4" and 0 < (f.get("height") or 0) <= min(max_height, 1080)
                and 0 < (f.get("width") or 0) <= 1920
                and f.get("dynamic_range") in (None, "SDR"))
    def audio_rank(f):
        note = str(f.get("format_note", "")).lower()
        return ("original" in note, f.get("language") == info.get("language") if info.get("language") else False,
                f.get("language_preference") or 0, "default" in note,
                f.get("abr") or f.get("tbr") or 0)
    audio = [f for f in formats if f.get("vcodec") == "none" and aac(f) and f.get("ext") == "m4a"]
    if not is_video:
        if audio:
            return [max(audio, key=audio_rank)]
        raise StreamError("対応するAAC音声がありません。保存型ダウンロードをご利用ください。")
    video = [f for f in formats if avc(f) and f.get("acodec") == "none"]
    if video and audio:
        return [max(video, key=lambda f: (f["height"], f.get("fps") or 0, f.get("tbr") or 0)),
                max(audio, key=audio_rank)]
    combined = [f for f in formats if avc(f) and aac(f)]
    if combined:
        return [max(combined, key=lambda f: (f["height"], f.get("fps") or 0, f.get("tbr") or 0))]
    raise StreamError("対応するH.264/AAC形式がありません。保存型ダウンロードをご利用ください。")


def mp4_boxes(data, start=0, end=None):
    """Iterate bounded ISO BMFF boxes (offset, payload, end, type)."""
    end = len(data) if end is None else end
    while start < end:
        if end - start < 8:
            raise StreamError("MP4の時刻情報を読み込めませんでした。")
        size = int.from_bytes(data[start:start + 4], "big")
        header = 8
        if size == 1:
            if end - start < 16:
                raise StreamError("MP4の時刻情報を読み込めませんでした。")
            size = int.from_bytes(data[start + 8:start + 16], "big")
            header = 16
        elif size == 0:
            size = end - start
        if size < header or start + size > end:
            raise StreamError("MP4の時刻情報を読み込めませんでした。")
        yield start, start + header, start + size, bytes(data[start + 4:start + 8])
        start += size


def mp4_timeline(chunks):
    """Move ffmpeg's empty edits into tfdt, which MSE can seek on.

    delay_moov preserves the original timestamps in edit lists. MSE only supports
    a single edit, so long empty edits must become decode timestamps instead.
    Only moov/moof are buffered; encoded mdat bytes pass through in bounded chunks.
    """
    offsets = {}
    pending = bytearray()
    remaining = 0
    unlimited = False

    def moov(data):
        children = list(mp4_boxes(data, 8))
        movie_scale = 0
        for _, p, _, kind in children:
            if kind == b"mvhd":
                i = p + (20 if data[p] else 12)
                movie_scale = int.from_bytes(data[i:i + 4], "big")
        if not movie_scale:
            raise StreamError("動画の再生時間を読み込めませんでした。")
        for _, p, end, kind in children:
            if kind != b"trak":
                continue
            track, scale, edits = None, 0, []
            for parent_box, cp, ce, ck in mp4_boxes(data, p, end):
                if ck == b"tkhd":
                    i = cp + (20 if data[cp] else 12)
                    track = int.from_bytes(data[i:i + 4], "big")
                elif ck == b"mdia":
                    for _, dp, _, dk in mp4_boxes(data, cp, ce):
                        if dk == b"mdhd":
                            i = dp + (20 if data[dp] else 12)
                            scale = int.from_bytes(data[i:i + 4], "big")
                elif ck == b"edts":
                    edits.extend((parent_box, box) for box in mp4_boxes(data, cp, ce) if box[3] == b"elst")
            if not track or not scale:
                raise StreamError("動画の時刻情報を読み込めませんでした。")
            for parent_box, (box, ep, ee, _) in edits:
                version = data[ep]
                count = int.from_bytes(data[ep + 4:ep + 8], "big")
                pos, empty = ep + 8, 0
                width = 8 if version == 1 else 4
                if version not in (0, 1) or count > 2:
                    raise StreamError("対応しないMP4時刻形式です。")
                for _ in range(count):
                    if pos + width * 2 + 4 > ee:
                        raise StreamError("MP4の時刻情報を読み込めませんでした。")
                    duration = int.from_bytes(data[pos:pos + width], "big")
                    media_time = int.from_bytes(data[pos + width:pos + width * 2], "big", signed=True)
                    rate = int.from_bytes(data[pos + width * 2:pos + width * 2 + 4], "big")
                    if rate != 65536:
                        raise StreamError("対応しないMP4再生速度です。")
                    if media_time == -1:
                        empty += duration
                    elif empty:
                        offset = round(empty * scale / movie_scale) - media_time
                        if offset < 0:
                            raise StreamError("MP4の再生位置を読み込めませんでした。")
                        offsets[track] = offset
                        data[parent_box + 4:parent_box + 8] = b"free"
                        break
                    pos += width * 2 + 4
        return data

    def moof(data):
        for _, p, end, kind in mp4_boxes(data, 8):
            if kind != b"traf":
                continue
            boxes = list(mp4_boxes(data, p, end))
            track = next((int.from_bytes(data[cp + 4:cp + 8], "big") for _, cp, _, ck in boxes if ck == b"tfhd"), None)
            for _, cp, ce, ck in boxes:
                if ck == b"tfdt" and track in offsets:
                    width = 8 if data[cp] == 1 else 4
                    if cp + 4 + width > ce:
                        raise StreamError("MP4の再生位置を読み込めませんでした。")
                    i = cp + 4
                    timestamp = int.from_bytes(data[i:i + width], "big") + offsets[track]
                    data[i:i + width] = timestamp.to_bytes(width, "big")
        return data

    try:
        for chunk in chunks:
            pending.extend(chunk)
            while pending:
                if remaining or unlimited:
                    size = len(pending) if unlimited else min(len(pending), remaining)
                    yield bytes(pending[:size])
                    del pending[:size]
                    remaining -= size if not unlimited else 0
                    continue
                if len(pending) < 8:
                    break
                size = int.from_bytes(pending[:4], "big")
                header = 8
                if size == 1:
                    if len(pending) < 16:
                        break
                    size = int.from_bytes(pending[8:16], "big")
                    header = 16
                kind = bytes(pending[4:8])
                if size and size < header:
                    raise StreamError("MP4の読み込みに失敗しました。")
                if kind in (b"moov", b"moof"):
                    if size < header or size > 2 * 1024 * 1024 or header != 8:
                        raise StreamError("MP4の時刻情報が大きすぎます。")
                    if len(pending) < size:
                        break
                    box = pending[:size]
                    del pending[:size]
                    yield bytes(moov(box) if kind == b"moov" else moof(box))
                else:
                    yield bytes(pending[:header])
                    del pending[:header]
                    unlimited = size == 0
                    remaining = max(0, size - header)
        if pending or remaining:
            raise StreamError("動画の配信が途中で終了しました。")
    finally:
        if hasattr(chunks, "close"):
            chunks.close()


@dataclass
class Media:
    sources: list
    fetched_at: float
    expires_at: float
    metadata: dict


@dataclass
class StreamState:
    lock: object = field(default_factory=threading.RLock)
    media: object = None
    sessions: int = 0
    error: str = ""


class Session:
    def __init__(self, manager, item):
        self.manager, self.item = manager, item
        self.process = self.upstream = self.http = self.timer = self.stderr_thread = None
        self.auth_failed = False
        self.offset = 0.0
        self.closed = False
        self.lock = threading.RLock()
        self.started = time.monotonic()
        self.bytes = 0
        self.reason = "client_disconnect"

    def stop_resources(self):
        with self.lock:
            self._stop_resources()

    def _stop_resources(self):
        if self.timer:
            self.timer.cancel()
        if self.process:
            process = self.process
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            if self.stderr_thread:
                self.stderr_thread.join(timeout=2)
            for pipe in (process.stdout, process.stderr):
                if pipe:
                    pipe.close()
            self.process = None
        if self.upstream:
            self.upstream.close()
            self.upstream = None
        if self.http:
            self.http.close()
            self.http = None

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            process = self.process
            self.stop_resources()
            exit_code = process.returncode if process else None
            self.manager.release(self)
            self.manager.log(f"stream end reason={self.reason} seconds={time.monotonic()-self.started:.1f} "
                             f"bytes={self.bytes} exit={exit_code}", self.item.uuid)

    def timeout(self):
        self.reason = "duration_limit"
        self.close()

    def drain_stderr(self, process):
        # Bound memory even when ffmpeg prints a large single line. Never log stderr.
        tail = b""
        while True:
            data = process.stderr.read1(4096)
            if not data:
                return
            tail = (tail + data)[-8192:]
            if re.search(rb"\b(?:401|403)\b", tail):
                self.auth_failed = True

    def start_remux(self, media):
        if self.closed:
            raise StreamError("配信準備の制限時間を超えました。再試行してください。")
        args = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "warning", "-copyts", "-start_at_zero"]
        for source in media.sources:
            if self.offset:
                args += ["-ss", f"{self.offset:.6f}"]
            headers = clean_headers(source.get("http_headers"))
            args += ["-rw_timeout", "20000000", "-protocol_whitelist", "https,http,tls,tcp,crypto",
                     "-headers", "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n", "-i", source["url"]]
        args += ["-map", "0:v:0", "-map", "1:a:0", "-c", "copy"]
        if media.metadata.get("is_live"):
            args += ["-bsf:a", "aac_adtstoasc"]
        args += ["-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov", "-flush_packets", "1",
                 "-frag_duration", "1000000", "-f", "mp4", "pipe:1"]
        with self.lock:
            if self.closed:
                raise StreamError("配信準備の制限時間を超えました。再試行してください。")
            if self.timer:
                self.timer.cancel()
            self.process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            process = self.process
            self.stderr_thread = threading.Thread(target=self.drain_stderr, args=(process,), daemon=True)
            self.stderr_thread.start()
            # No HTTP 200 until ffmpeg actually produces output. Bound a stalled start.
            remaining = self.manager.duration - (time.monotonic() - self.started)
            self.timer = threading.Timer(max(.01, min(45, remaining)), self.timeout)
            self.timer.daemon = True
            self.timer.start()
        first = process.stdout.read1(self.manager.chunk_size)
        if not first:
            process.wait(timeout=2)
            self.stderr_thread.join(timeout=2)
            raise StreamError("配信の開始に失敗しました。再試行または保存型ダウンロードをご利用ください。")
        self.timer.cancel()
        if self.closed:
            raise StreamError("配信準備の制限時間を超えました。再試行してください。")
        self.arm_duration()
        self.manager.log(f"ffmpeg started seconds={time.monotonic()-self.started:.2f}", self.item.uuid)
        return first

    def arm_duration(self):
        self.timer = threading.Timer(max(0.01, self.manager.duration - (time.monotonic()-self.started)), self.timeout)
        self.timer.daemon = True
        self.timer.start()

    def remux_body(self, first):
        body = self.raw_remux_body(first)
        try:
            yield from mp4_timeline(body) if self.offset else body
        finally:
            body.close()

    def raw_remux_body(self, first):
        process = self.process
        try:
            if self.closed:
                return
            self.bytes += len(first)
            yield first
            while not self.closed:
                data = process.stdout.read1(self.manager.chunk_size)
                if not data:
                    code = process.wait(timeout=2)
                    self.reason = "completed" if code == 0 else "upstream_failure"
                    if code:
                        self.item.stream_state.error = "配信が中断されました。再試行または保存型ダウンロードをご利用ください。"
                        raise StreamError(self.item.stream_state.error)
                    break
                self.bytes += len(data)
                yield data
        except (OSError, ValueError, subprocess.TimeoutExpired):
            if not self.closed:
                self.reason = "upstream_failure"
                self.item.stream_state.error = "配信が中断されました。再試行してください。"
                raise StreamError(self.item.stream_state.error) from None
        finally:
            self.close()

    def proxy_body(self):
        try:
            if self.closed:
                return
            upstream = self.upstream
            for data in upstream.iter_content(self.manager.chunk_size):
                if self.closed:
                    break
                if data:
                    self.bytes += len(data)
                    yield data
            if not self.closed:
                self.reason = "completed"
        except (requests.RequestException, OSError, ValueError):
            if self.closed:
                return
            self.reason = "upstream_failure"
            self.item.stream_state.error = "音声または動画の配信が中断されました。再試行してください。"
            raise StreamError(self.item.stream_state.error) from None
        finally:
            self.close()


class StreamManager:
    def __init__(self, config, log):
        self.enabled = config["enabled"]
        self.max_sessions = config["max_sessions"]
        self.duration = config["max_duration"]
        self.height = config["max_height"]
        self.chunk_size = config["chunk_size"]
        self.margin = config["refresh_margin"]
        self.provider, self.bind_ip = config["provider"], config["bind_ip"]
        self.log = log
        self.lock = threading.RLock()
        self.sessions = set()
        self.shutting_down = False
        atexit.register(self.close)

    def close(self):
        with self.lock:
            self.shutting_down = True
            sessions = list(self.sessions)
        for session in sessions:
            session.reason = "app_shutdown"
            session.close()

    def reserve(self, item):
        with self.lock:
            if self.shutting_down:
                raise StreamError("配信サービスは停止中です。", 503)
            if len(self.sessions) >= self.max_sessions:
                raise StreamError("同時配信数の上限です。しばらくして再試行してください。", 429)
            session = Session(self, item)
            self.sessions.add(session)
            item.stream_state.sessions += 1
            session.arm_duration()
            self.log(f"stream sessions={len(self.sessions)}", item.uuid)
            return session

    def release(self, session):
        with self.lock:
            self.sessions.discard(session)
            session.item.stream_state.sessions -= 1

    def invalidate(self, item, media):
        with item.stream_state.lock:
            if item.stream_state.media is media:
                item.stream_state.media = None

    def media(self, item, rejected=None):
        state = item.stream_state
        with state.lock:
            now = time.time()
            if state.media and state.media is not rejected and now < state.media.expires_at - self.margin:
                return state.media
            state.media = None
            started = time.monotonic()
            try:
                source_url = normalize_source_url(item.url)
                if not valid_source_url(source_url):
                    raise StreamError("直接再生にはYouTubeの動画URLを指定してください。")
                with YoutubeDL(ytdlp_options(self.provider, self.bind_ip)) as ydl:
                    info = ydl.extract_info(source_url, download=False, process=False) or {}
                    if "entries" in info:
                        entry = next((e for e in info["entries"] if e), {})
                        entry_url = entry.get("webpage_url") or entry.get("url")
                        if not valid_source_url(entry_url):
                            raise StreamError("YouTubeの検索結果を取得できませんでした。")
                        info = ydl.extract_info(entry_url, download=False, process=False) or {}
                if not str(info.get("extractor_key", info.get("extractor", ""))).lower().startswith("youtube"):
                    raise StreamError("直接再生はYouTubeの通常動画に対応しています。保存型ダウンロードをご利用ください。")
                sources = select_formats(info, item.is_video, self.height)
                sources = [dict(s, http_headers=clean_headers({**info.get("http_headers", {}), **s.get("http_headers", {})})) for s in sources]
                expiry = []
                for source in sources:
                    value = parse_qs(urlparse(source["url"]).query).get("expire", [None])[0]
                    try:
                        expiry.append(float(value))
                    except (TypeError, ValueError):
                        expiry.append(now + 900)
                # Keep only public metadata on video_item. URLs and headers live in Media.
                fields = ("id", "title", "uploader", "description", "webpage_url", "upload_date", "view_count",
                          "duration", "duration_string", "width", "height", "fps", "language")
                metadata = {k: info[k] for k in fields if k in info}
                metadata["is_live"] = bool(info.get("is_live") or info.get("live_status") == "is_live")
                metadata.update({k: sources[0].get(k) for k in ("width", "height", "fps", "vcodec", "ext")})
                if isinstance(metadata.get("duration"), (int, float)) and math.isfinite(metadata["duration"]):
                    seconds = max(0, int(metadata["duration"]))
                    metadata["duration_string"] = f"{seconds // 3600}:{seconds // 60 % 60:02}:{seconds % 60:02}" if seconds >= 3600 else f"{seconds // 60}:{seconds % 60:02}"
                metadata["acodec"] = sources[-1].get("acodec")
                metadata["language"] = sources[-1].get("language")
                metadata["format_id"] = "+".join(str(s.get("format_id", "")) for s in sources)
                media = Media(sources, now, min(expiry), metadata)
                state.media, state.error = media, ""
                item.info = metadata
                self.log(f"stream extract video={metadata.get('id')} formats={metadata['format_id']} "
                         f"height={metadata.get('height')} codec={metadata.get('vcodec')}/{metadata.get('acodec')} "
                         f"seconds={time.monotonic()-started:.2f}", item.uuid)
                return media
            except StreamError as error:
                state.error = str(error)
                raise
            except Exception:
                # Do not stringify upstream exceptions: they can include credentials.
                state.error = "動画情報の取得に失敗しました。再試行または保存型ダウンロードをご利用ください。"
                raise StreamError(state.error, 503 if not self.provider_available() else 502) from None

    def provider_available(self):
        try:
            with requests.get(self.provider.rstrip("/") + "/ping", timeout=3) as response:
                return response.ok
        except requests.RequestException:
            return False

    def diagnostics(self):
        try:
            plugin = version("bgutil-ytdlp-pot-provider")
        except PackageNotFoundError:
            plugin = "missing"
        server = "unavailable"
        try:
            with requests.get(self.provider.rstrip("/") + "/ping", timeout=3) as response:
                if response.ok:
                    data = response.json()
                    value = data.get("version", "unknown")
                    server = value if re.fullmatch(r"[A-Za-z0-9.+_-]{1,64}", str(value)) else "unknown"
        except (requests.RequestException, ValueError, AttributeError):
            pass
        self.log(f"PO Token plugin={plugin} server={server} remux_enabled={self.enabled}")

    def response(self, item):
        if not item.play_directly or item.status != "completed":
            raise StreamError("このアイテムは直接再生の準備ができていません。", 409)
        if not self.enabled:
            raise StreamError("直接再生は現在無効です。保存型ダウンロードをご利用ください。", 503)
        try:
            offset = float(request.args.get("start", "0"))
        except ValueError:
            raise StreamError("再生位置が正しくありません。", 400) from None
        if not math.isfinite(offset) or offset < 0:
            raise StreamError("再生位置が正しくありません。", 400)
        session = self.reserve(item)
        session.offset = offset
        headers = {"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff", "X-Accel-Buffering": "no"}
        try:
            media = self.media(item)
            if media.metadata.get("is_live") and offset:
                raise StreamError("ライブ配信では再生位置を指定できません。", 400)
            duration = media.metadata.get("duration")
            if offset and (not isinstance(duration, (int, float)) or not math.isfinite(duration) or offset >= duration):
                raise StreamError("動画の範囲内の再生位置を指定してください。", 400)
            if isinstance(duration, (int, float)) and math.isfinite(duration) and duration > 0:
                headers["X-Stream-Duration"] = str(duration)
            for attempt in range(2):
                if len(media.sources) == 2:
                    try:
                        first = session.start_remux(media)
                        response = Response(session.remux_body(first), mimetype="video/mp4",
                                            headers={**headers, "Accept-Ranges": "none", "X-Stream-Mode": "remux",
                                                     "X-Stream-Live": "1" if media.metadata.get("is_live") else "0",
                                                     "X-Stream-Codecs": f"{media.sources[0]['vcodec']},{media.sources[1]['acodec']}"})
                        break
                    except StreamError:
                        session.stop_resources()
                        if session.closed or not session.auth_failed or attempt:
                            if session.auth_failed:
                                self.invalidate(item, media)
                            raise
                        session.auth_failed = False
                        session.arm_duration()
                        media = self.media(item, rejected=media)
                        continue
                if session.closed:
                    raise StreamError("配信準備の制限時間を超えました。再試行してください。")
                range_header = request.headers.get("Range")
                if range_header and not re.fullmatch(r"bytes=(?:\d+-\d*|-\d+)", range_header):
                    raise StreamError("単一のバイト範囲を指定してください。", 416)
                source = media.sources[0]
                upstream_headers = clean_headers(source.get("http_headers"))
                if range_header:
                    upstream_headers["Range"] = range_header
                with session.lock:
                    if session.closed:
                        raise StreamError("配信準備の制限時間を超えました。再試行してください。")
                    session.http = requests.Session()
                    session.http.trust_env = False
                    http = session.http
                upstream = http.get(source["url"], headers=upstream_headers,
                                    stream=True, timeout=(10, 20), allow_redirects=False)
                with session.lock:
                    if session.closed:
                        upstream.close()
                        raise StreamError("配信準備の制限時間を超えました。再試行してください。")
                    session.upstream = upstream
                if session.upstream.status_code in (401, 403) and not attempt:
                    session.stop_resources()
                    session.arm_duration()
                    media = self.media(item, rejected=media)
                    continue
                if session.upstream.status_code == 416:
                    if re.fullmatch(r"bytes \*/\d+", session.upstream.headers.get("Content-Range", "")):
                        headers["Content-Range"] = session.upstream.headers["Content-Range"]
                    response = Response(status=416, headers=headers)
                    session.close()
                    return response
                if session.upstream.status_code not in (200, 206):
                    if session.upstream.status_code in (401, 403):
                        self.invalidate(item, media)
                    raise StreamError("上流の配信開始に失敗しました。再試行してください。")
                for key in ("Content-Length", "Content-Range", "Accept-Ranges"):
                    value = session.upstream.headers.get(key)
                    if value:
                        headers[key] = value
                session.arm_duration()
                headers["X-Stream-Mode"] = "proxy"
                response = Response(session.proxy_body(), status=session.upstream.status_code,
                                    mimetype="video/mp4" if item.is_video else "audio/mp4", headers=headers)
                break
            # Handles close even if WSGI never advances the generator (e.g. HEAD).
            response.call_on_close(session.close)
            return response
        except StreamError as error:
            item.stream_state.error = str(error)
            session.reason = "start_failure"
            session.close()
            raise
        except Exception:
            session.reason = "start_failure"
            session.close()
            item.stream_state.error = "配信の開始に失敗しました。再試行または保存型ダウンロードをご利用ください。"
            raise StreamError(item.stream_state.error) from None


def register_stream_routes(app, system):
    @app.route("/stream/<uuid>", methods=["GET"])
    def stream(uuid):
        item = system.video_dic.get(uuid)
        if item is None:
            return {"error": "そのIDは存在しません"}, 404, {"Cache-Control": "no-store, private"}
        try:
            # HEAD must not launch ffmpeg or reserve a viewing session.
            if request.method == "HEAD":
                if not system.streams.enabled:
                    raise StreamError("直接再生は現在無効です。", 503)
                if not item.play_directly or item.status != "completed":
                    raise StreamError("直接再生の準備ができていません。", 409)
                return Response(mimetype="video/mp4" if item.is_video else "audio/mp4",
                                headers={"Cache-Control": "no-store, private", "X-Content-Type-Options": "nosniff"})
            return system.streams.response(item)
        except StreamError as error:
            return {"error": str(error)}, error.status, {"Cache-Control": "no-store, private"}
