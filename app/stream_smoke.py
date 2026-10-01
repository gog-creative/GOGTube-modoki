"""Administrator CLI smoke check; no signed URLs or upstream stderr are printed."""
import argparse
import os
import sys
from types import SimpleNamespace
from flask import Flask
from streaming import StreamManager, StreamState


def run_smoke(url, seconds=15):
    config = {
        "enabled": True, "max_sessions": 1, "max_duration": max(60, seconds + 45),
        "max_height": int(os.environ.get("STREAM_MAX_HEIGHT", "1080")), "chunk_size": 65536,
        "refresh_margin": 300,
        "provider": os.environ.get("POT_PROVIDER", "http://ytmp3modoki2-bgutil-provider-1:4416"),
        "bind_ip": os.environ.get("DOWNLOAD_BIND_IP", "0.0.0.0"),
    }
    manager = StreamManager(config, lambda message, uuid=None: print(message, flush=True))
    item = SimpleNamespace(uuid="admin-smoke", url=url, is_video=True, play_directly=True,
                           status="completed", stream_state=StreamState(), info={})
    response = None
    try:
        import time
        app = Flask(__name__)
        with app.test_request_context("/stream/admin-smoke"):
            response = manager.response(item)
            started = time.monotonic()
            received = 0
            prefix = b""
            for chunk in response.response:
                prefix = (prefix + chunk)[:4096]
                received += len(chunk)
                if time.monotonic() - started >= seconds:
                    break
            if not received or b"ftyp" not in prefix:
                raise RuntimeError("invalid_mp4")
            print(f"STREAM_SMOKE_OK bytes={received} formats={item.info.get('format_id')} height={item.info.get('height')}", flush=True)
            return True
    except Exception as error:
        print(f"STREAM_SMOKE_FAILED type={type(error).__name__}", file=sys.stderr, flush=True)
        return False
    finally:
        if response:
            response.close()
        manager.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("url", nargs="?", default=os.environ.get("STREAM_SMOKE_URL"))
    parser.add_argument("--seconds", type=int, default=15)
    args = parser.parse_args()
    if not args.url:
        parser.error("Set STREAM_SMOKE_URL or supply a YouTube URL")
    sys.exit(0 if run_smoke(args.url, max(1, args.seconds)) else 1)
