import os
import signal
import subprocess
import time

command = ["python3", "-m", "gunicorn", "-b", "0.0.0.0:80", "--workers", "1",
           "--threads", "4", "-k", "gevent", "--graceful-timeout", "15",
           "--config", "/app/gunicorn.conf.py", "frontend:app"]
if os.environ.get("ADMIN_DEBUG", "").lower() == "true":
    command += ["--log-level", "debug", "--error-logfile=-", "--access-logfile=-", "--capture-output"]

process = None
stopping = False


def stop_child():
    if process and process.poll() is None:
        # QUIT exits workers immediately, allowing their streaming cleanup hook to run.
        process.send_signal(signal.SIGQUIT)
        try:
            process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def shutdown(signum, frame):
    global stopping
    stopping = True
    stop_child()


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    enabled = os.environ.get("STREAM_REMUX_ENABLED", "false")
    while not stopping:
        print("Starting GOGTube", flush=True)
        process = subprocess.Popen(command, cwd="/app")
        deadline = time.monotonic() + 24 * 60 * 60
        while not stopping and process.poll() is None and time.monotonic() < deadline:
            time.sleep(1)
        if stopping:
            break
        expired = time.monotonic() >= deadline
        stop_child()
        if not expired:
            print(f"GUNICORN_EXITED code={process.returncode}; restarting in 5 seconds", flush=True)
            time.sleep(5)
            continue
        print("Updating yt-dlp and PO Token plugin", flush=True)
        result = subprocess.run(["python3", "-m", "pip", "install", "--pre", "-U",
                                 "yt-dlp", "yt-dlp-ejs", "bgutil-ytdlp-pot-provider"])
        if result.returncode:
            print("STREAM_DEPENDENCY_UPDATE_FAILED", flush=True)
        smoke_url = os.environ.get("STREAM_SMOKE_URL")
        if smoke_url:
            # A fresh interpreter imports the newly installed extractor/plugin versions.
            smoke = subprocess.run(["python3", "/app/stream_smoke.py", smoke_url], cwd="/app")
            if smoke.returncode == 0:
                os.environ["STREAM_REMUX_ENABLED"] = enabled
            else:
                os.environ["STREAM_REMUX_ENABLED"] = "false"
                print("STREAM_DISABLED_AFTER_SMOKE_FAILURE: administrator action required", flush=True)
        else:
            print("STREAM_SMOKE_NOT_CONFIGURED: set STREAM_SMOKE_URL for update verification", flush=True)
