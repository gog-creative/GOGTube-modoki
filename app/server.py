import subprocess
from time import sleep

command = "python3 -m gunicorn -b 0.0.0.0:80 --workers 1 --threads 3 -k gevent frontend:app"
while True:
    print("Starting YouTubeもどきもどき")
    process = subprocess.Popen(command,shell=True,cwd="/app")
    sleep(24*60*60)
    print("Starting Packages UPDATE")
    process.kill()
    subprocess.run("apt update && apt upgrade -y",shell=True)
    subprocess.run("pip install --pre -U yt-dlp", shell=True)