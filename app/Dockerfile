FROM python:3-alpine AS builder

WORKDIR /app

COPY frontend.py /app/
COPY core.py /app/
COPY server.py /app/
COPY requirements.txt /app/
COPY templates/ /app/templates
COPY static/ /app/static

FROM python:3-alpine AS dev
WORKDIR /app

COPY --from=builder /app /app

#一般設定
ENV ADMIN_LOGIN=admin
ENV ADMIN_PASSWORD=password
ENV ADMIN_NEW_REQUEST=True
ENV ADMIN_OAUTH=False
ENV DOWNLOAD_THREADS=2
ENV DOWNLOAD_BIND_IP=0.0.0.0
ENV STORAGE_TIME_FORMAT='%Y/%m/%d %H:%M:%S'
ENV STORAGE_LIMIT_SIZE=3000
ENV STORAGE_AUTO_DELETE=True
ENV TZ=Asia/Tokyo

EXPOSE 80

RUN python -m pip install -r /app/requirements.txt\
    && python -m pip install --pre yt-dlp\
    && apk update\
    && apk add ffmpeg

CMD ["python", "server.py"]