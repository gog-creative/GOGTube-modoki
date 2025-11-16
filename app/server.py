import subprocess
from time import sleep
from os import environ

command = "python3 -m gunicorn -b 0.0.0.0:80 --workers 1 --threads 4 -k gevent frontend:app"
if environ.get("ADMIN_DEBUG","").lower() == "true":
    print("!!! DEBUG MODE IS ENABLED !!!", flush=True)
    command+=" --log-level debug --error-logfile=- --access-logfile=- --capture-output"

while True:
    print("Starting YouTubeもどきもどき", flush=True)
    process = subprocess.Popen(command,shell=True,cwd="/app")
    sleep(24*60*60)
    print("Starting Packages UPDATE",flush=True)
    process.kill()
    subprocess.run("apt update && apt upgrade -y",shell=True)
    subprocess.run("pip install --pre -U yt-dlp", shell=True)