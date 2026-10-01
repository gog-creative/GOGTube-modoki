import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from flask import Flask, render_template
import streaming as s


def source(fid, height=None, **values):
    video = height is not None
    return dict(format_id=fid, url=f"https://r1.googlevideo.com/videoplayback?expire={int(time.time())+7200}&sig=SECRET",
                protocol="https", vcodec="avc1.640028" if video else "none", acodec="none" if video else "mp4a.40.2",
                ext="mp4" if video else "m4a", width=1920 if video else None, height=height,
                **values)


def item(video=True):
    return SimpleNamespace(uuid="test", url="https://www.youtube.com/watch?v=test", is_video=video,
                           play_directly=True, status="completed", stream_state=s.StreamState(), info={})


def manager(**config):
    defaults = dict(enabled=True, max_sessions=2, max_duration=60, max_height=1080,
                    chunk_size=4096, refresh_margin=300, provider="http://provider:4416", bind_ip=None)
    defaults.update(config)
    return s.StreamManager(defaults, lambda *a: None)


class SelectionTests(unittest.TestCase):
    def test_fallback_heights(self):
        for heights, expected in [([1080, 720, 480, 360], 1080), ([720,480,360],720), ([480,360],480), ([360],360)]:
            info = {"formats": [source(str(h),h) for h in heights]+[source("audio")]}
            selected = s.select_formats(info)
            self.assertEqual(selected[0]["height"], expected)
            self.assertEqual(selected[1]["format_id"], "audio")
        self.assertEqual(s.select_formats(info, max_height=360)[0]["height"],360)

    def test_reject_incompatible_and_upcoming(self):
        for modifications in [{"vcodec":"vp9"}, {"vcodec":"av01"}, {"dynamic_range":"HDR10"},
                              {"height":2160}, {"width":2048}, {"has_drm":True}]:
            v = source("video",1080);v.update(modifications)
            with self.assertRaises(s.StreamError): s.select_formats({"formats":[v,source("a")]})
        a = source("a");a["acodec"]="opus"
        with self.assertRaises(s.StreamError):s.select_formats({"formats":[source("v",720),a]})
        with self.assertRaises(s.StreamError):s.select_formats({"formats":[],"live_status":"is_upcoming"})

    def test_live_hls_selection(self):
        video = source('232',720)
        video.update(url='https://manifest.googlevideo.com/api/manifest/hls_playlist', protocol='m3u8_native')
        audio = source('234')
        audio.update(url='https://manifest.googlevideo.com/api/manifest/hls_playlist', protocol='m3u8_native', acodec=None)
        selected = s.select_formats({'formats':[video,audio], 'live_status':'is_live'})
        self.assertEqual([f['format_id'] for f in selected], ['232','234'])
        self.assertEqual(selected[1]['acodec'],'mp4a.40.2')
        with self.assertRaises(s.StreamError):s.select_formats({'formats':[video,audio], 'live_status':'is_live'},False)

    def test_original_audio_and_combined(self):
        original=source("orig",format_note="original",language="ja",abr=64)
        dubbed=source("dub",language="en",language_preference=10,abr=128)
        self.assertEqual(s.select_formats({"formats":[source("v",1080),dubbed,original]})[-1]["format_id"],"orig")
        v=source("combined",720);v["acodec"]="mp4a.40.2"
        self.assertEqual(len(s.select_formats({"formats":[v]})),1)
        self.assertEqual(s.select_formats({"formats":[original]},False),[original])

    def test_url_and_headers(self):
        for url in ["http://r.googlevideo.com/a","https://googlevideo.com.evil/a","https://127.0.0.1/a","file:///tmp/a","https://user@r.googlevideo.com/a"]:
            self.assertFalse(s.valid_media_url(url))
        self.assertFalse(s.valid_source_url("https://youtube.com.evil/a"))
        self.assertFalse(s.valid_source_url("http://127.0.0.1"))
        google = 'https://www.google.com/url?url=https%3A%2F%2Fwww.youtube.com%2Fwatch%3Fv%3Dabc'
        self.assertEqual(s.normalize_source_url(google),'https://www.youtube.com/watch?v=abc')
        self.assertEqual(s.normalize_source_url('https://www.google.com/url?url=http%3A%2F%2F127.0.0.1'),
                         'https://www.google.com/url?url=http%3A%2F%2F127.0.0.1')
        self.assertEqual(s.clean_headers({"Referer":"https://youtube.com", "Range":"bytes=0-", "Cookie":"bad\r\nInjected: a"}),{"Referer":"https://youtube.com"})
        self.assertEqual(s.ytdlp_options("http://provider")["extractor_args"]["youtubepot-bgutilhttp"]["base_url"],["http://provider"])
        self.assertFalse(s.ytdlp_options("http://provider")["debug_printtraffic"])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.manager=manager();self.item=item();self.calls=0
        test=self
        class YDL:
            def __init__(self, options): pass
            def __enter__(self): return self
            def __exit__(self,*args): pass
            def extract_info(self,*args,**kwargs):
                test.calls+=1;time.sleep(.02)
                return {"extractor_key":"Youtube", "id":"test","title":"Title", "formats":[source("v",1080),source("a")]}
        self.patch=patch.object(s,"YoutubeDL",YDL);self.patch.start()
    def tearDown(self):self.patch.stop();self.manager.close()
    def test_cache_refresh_and_public_metadata(self):
        first=self.manager.media(self.item)
        self.assertIs(self.manager.media(self.item),first)
        first.expires_at=time.time()+299
        second=self.manager.media(self.item)
        self.assertIsNot(first,second)
        self.assertEqual(self.calls,2)
        self.assertNotIn("SECRET",json.dumps(self.item.info))
        self.assertNotIn("googlevideo",json.dumps(self.item.info))
    def test_concurrent_extraction_and_rejection(self):
        results=[]
        threads=[threading.Thread(target=lambda:results.append(self.manager.media(self.item))) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(self.calls,1)
        old=results[0];results.clear()
        threads=[threading.Thread(target=lambda:results.append(self.manager.media(self.item,rejected=old))) for _ in range(5)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(self.calls,2)
    def test_exception_redaction(self):
        with patch.object(s,"YoutubeDL",side_effect=RuntimeError("https://r.googlevideo.com/SECRET")), patch.object(self.manager,"provider_available",return_value=False):
            with self.assertRaises(s.StreamError) as error:self.manager.media(self.item)
        self.assertEqual(error.exception.status,503)
        self.assertNotIn("SECRET",str(error.exception))


class EndpointTests(unittest.TestCase):
    def setUp(self):
        self.manager=manager(max_sessions=1);self.item=item()
        self.app=Flask(__name__,template_folder=str(Path(__file__).resolve().parents[1]/"templates"))
        self.app.add_url_rule('/api/status/<uuid>',endpoint='status_api',view_func=lambda uuid:{})
        self.app.jinja_env.filters['int_f']=str
        s.register_stream_routes(self.app,SimpleNamespace(streams=self.manager,video_dic={"test":self.item}))
        self.client=self.app.test_client()
    def tearDown(self):self.manager.close()
    def test_status_and_head(self):
        self.assertEqual(self.client.get('/stream/missing').status_code,404)
        self.item.status='queue';self.assertEqual(self.client.get('/stream/test').status_code,409)
        self.item.status='completed';self.manager.enabled=False
        self.assertEqual(self.client.get('/stream/test').status_code,503)
        self.manager.enabled=True
        with patch.object(self.manager,'media') as extract:
            self.assertEqual(self.client.head('/stream/test').status_code,200)
            extract.assert_not_called()
        session=self.manager.reserve(self.item)
        self.assertEqual(self.client.get('/stream/test').status_code,429)
        session.close();session.close();self.assertEqual(self.item.stream_state.sessions,0)
    def test_template_origin_and_fallback(self):
        with self.app.test_request_context('/streaming/test'):
            html=render_template('status.html',item=self.item,output=dict(status='completed',info={},message='ready',stream_enabled=True,stream_url='/stream/test'))
            self.assertIn('src="/stream/test"',html)
            self.assertIn('保存型ダウンロード',html)
            self.assertNotIn('googlevideo',html)
            self.item.is_video=False
            html=render_template('status.html',item=self.item,output=dict(status='completed',info={},message='ready',stream_enabled=True,stream_url='/stream/test'))
            self.assertIn('<audio',html)
            self.assertIn('value="audio"',html)
    def test_proxy_range_retry_and_close(self):
        self.item.is_video=False
        media=s.Media([source('a')],time.time(),time.time()+7200,{})
        rejected=[]
        def extract(item,rejected=None):
            if rejected:rejected_list.append(rejected)
            return media
        rejected_list=[]
        class Upstream:
            def __init__(self,status):self.status_code=status;self.headers={"Content-Length":"4","Content-Range":"bytes 0-3/10","Accept-Ranges":"bytes"};self.closed=False
            def close(self):self.closed=True
            def iter_content(self,size):yield b'abcd'
        responses=[Upstream(403),Upstream(206)]
        http=SimpleNamespace(get=unittest.mock.Mock(side_effect=responses),close=lambda:None)
        with patch.object(self.manager,'media',side_effect=extract),patch.object(s.requests,'Session',return_value=http):
            response=self.client.get('/stream/test',headers={'Range':'bytes=0-3'})
            self.assertEqual(response.status_code,206);self.assertEqual(response.data,b'abcd')
            self.assertEqual(response.headers['Content-Range'],'bytes 0-3/10')
            self.assertEqual(http.get.call_args.kwargs['headers']['Range'],'bytes=0-3')
            response.close()
        self.assertEqual(len(rejected_list),1)
        self.assertTrue(all(r.closed for r in responses))
        self.assertFalse(self.manager.sessions)
    def test_remux_retry_once(self):
        media=s.Media([source('v',720),source('a')],0,time.time()+7200,{})
        calls=[]
        def start(session,media):
            calls.append(1);session.auth_failed=True
            raise s.StreamError('start failed')
        with patch.object(self.manager,'media',return_value=media) as extract,patch.object(s.Session,'start_remux',start):
            self.assertEqual(self.client.get('/stream/test').status_code,502)
            self.assertEqual(len(calls),2)
            self.assertEqual(extract.call_count,2)
        self.assertFalse(self.manager.sessions)
    def test_seek_offset_validation_and_duration(self):
        media=s.Media([source('v',720),source('a')],0,time.time()+7200,{'duration':120})
        offsets=[]
        def start(session,media):
            offsets.append(session.offset)
            return b'ftyp'
        with patch.object(self.manager,'media',return_value=media),patch.object(s.Session,'start_remux',start),patch.object(s.Session,'remux_body',return_value=iter([b'ftyp'])):
            for value in ('nan','inf','-1','abc','120','121'):
                self.assertEqual(self.client.get('/stream/test?start='+value).status_code,400)
                self.assertFalse(self.manager.sessions)
            response=self.client.get('/stream/test?start=75.25')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['X-Stream-Duration'],'120')
            self.assertEqual(response.data,b'ftyp')
            response.close()
        self.assertEqual(offsets,[75.25])
        self.assertFalse(self.manager.sessions)

    def test_live_response_has_no_server_seek(self):
        media=s.Media([source('270',1080),source('234')],0,time.time()+7200,{'is_live':True})
        with patch.object(self.manager,'media',return_value=media),patch.object(s.Session,'start_remux',return_value=b'ftyp'),patch.object(s.Session,'remux_body',return_value=iter([b'ftyp'])):
            self.assertEqual(self.client.get('/stream/test?start=5').status_code,400)
            response=self.client.get('/stream/test')
            self.assertEqual(response.status_code,200)
            self.assertEqual(response.headers['X-Stream-Live'],'1')
            response.close()
        self.assertFalse(self.manager.sessions)

    def test_invalid_range(self):
        self.item.is_video=False
        with patch.object(self.manager,'media',return_value=s.Media([source('a')],0,9999999999,{})):
            self.assertEqual(self.client.get('/stream/test',headers={'Range':'bytes=0-2,4-6'}).status_code,416)
        self.assertFalse(self.manager.sessions)


class FFmpegTests(unittest.TestCase):
    def setUp(self):self.manager=manager();self.item=item()
    def tearDown(self):self.manager.close()
    def test_real_remux_disconnect_and_mp4(self):
        real_popen=subprocess.Popen
        with tempfile.TemporaryDirectory() as tmp:
            video=Path(tmp)/'video.mp4';audio=Path(tmp)/'audio.m4a'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','testsrc2=size=320x180:rate=15','-t','12','-an','-c:v','libx264','-g','15',str(video)],check=True)
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440','-t','12','-vn','-c:a','aac',str(audio)],check=True)
            def fixture_popen(args,**kwargs):
                args=list(args);inputs=iter([video,audio])
                for index,value in enumerate(args):
                    if value=='-i':args[index+1]=str(next(inputs))
                    if value=='-protocol_whitelist':args[index+1]='file,https,tls,tcp'
                for option in ('-headers', '-rw_timeout'):
                    while option in args:
                        index=args.index(option);del args[index:index+2]
                return real_popen(args,**kwargs)
            session=self.manager.reserve(self.item)
            media=s.Media([source('v',180),source('a')],0,9999999999,{})
            with patch.object(s.subprocess,'Popen',side_effect=fixture_popen):
                first=session.start_remux(media);process=session.process
                body=session.remux_body(first);data=b''.join(body)
            self.assertIn(b'ftyp',data[:64]);self.assertIn(b'moof',data)
            result=subprocess.run(['ffprobe','-v','error','-show_streams','-of','json','pipe:0'],input=data,stdout=subprocess.PIPE,check=True)
            codecs={stream['codec_name'] for stream in json.loads(result.stdout)['streams']}
            self.assertEqual(codecs,{'h264','aac'})
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()),['audio.m4a','video.mp4'])
            self.assertIsNotNone(process.poll());self.assertFalse(self.manager.sessions)
            session=self.manager.reserve(self.item);session.offset=5.5
            with patch.object(s.subprocess,'Popen',side_effect=fixture_popen):
                first=session.start_remux(media)
                seek_data=b''.join(session.remux_body(first))
            self.assertEqual(b''.join(s.mp4_timeline(seek_data[i:i+7] for i in range(0,len(seek_data),7))),seek_data)
            packets=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_packets','-of','json','pipe:0'],input=seek_data))['packets']
            first_video=next(float(p['pts_time']) for p in packets if p['codec_type']=='video')
            first_audio=next(float(p['pts_time']) for p in packets if p['codec_type']=='audio')
            # Seeked fMP4 must keep the original timeline rather than relabeling frames as zero.
            self.assertGreaterEqual(first_video,4.5);self.assertLessEqual(first_video,5.5)
            self.assertGreaterEqual(first_audio,5.4);self.assertLess(first_audio,5.7)
            self.assertFalse(self.manager.sessions)
            # Slow child ensures response.close must actually terminate a running process.
            script="import sys,time;sys.stdout.buffer.write(b'ftyp'+b'x'*8192);sys.stdout.flush();time.sleep(60)"
            with patch.object(s.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen(['python3','-c',script],**kw)):
                session=self.manager.reserve(self.item);first=session.start_remux(media);process=session.process
                body=session.remux_body(first);next(body);body.close()
                self.assertIsNotNone(process.poll());self.assertFalse(self.manager.sessions)
    def test_stderr_backpressure_and_duration(self):
        real_popen=subprocess.Popen
        script="import sys,time;sys.stderr.buffer.write(b'x'*1000000);sys.stderr.flush();sys.stdout.buffer.write(b'ftyp'+b'x'*8192);sys.stdout.flush();time.sleep(60)"
        media=s.Media([source('v',720),source('a')],0,9999999999,{})
        with patch.object(s.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen(['python3','-c',script],**kw)):
            session=self.manager.reserve(self.item);first=session.start_remux(media);process=session.process
            self.assertTrue(first)
            session.timeout()
            self.assertIsNotNone(process.poll());self.assertFalse(self.manager.sessions)

    def test_start_timeout_and_cooperative_read(self):
        real_popen=subprocess.Popen
        media=s.Media([source('v',720),source('a')],0,9999999999,{})
        count=[];done=threading.Event()
        def heartbeat():
            while not done.wait(.01):count.append(1)
        beat=threading.Thread(target=heartbeat);beat.start()
        script="import sys,time;time.sleep(.2);sys.stdout.buffer.write(b'ftyp'+b'x'*8192);sys.stdout.flush();time.sleep(60)"
        try:
            with patch.object(s.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen(['python3','-c',script],**kw)):
                session=self.manager.reserve(self.item);session.start_remux(media);process=session.process
                self.assertGreaterEqual(len(count),5)
                session.close();self.assertIsNotNone(process.poll())
        finally:done.set();beat.join()
        self.manager.duration=1
        with patch.object(s.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen(['python3','-c','import time;time.sleep(60)'],**kw)):
            session=self.manager.reserve(self.item)
            with self.assertRaises(s.StreamError):session.start_remux(media)
            self.assertTrue(session.closed);session.close();self.assertFalse(self.manager.sessions)

    def test_auth_failure_is_detected_from_stderr(self):
        real_popen=subprocess.Popen
        script="import sys;sys.stderr.write('HTTP error 403 Forbidden');sys.stderr.flush();sys.exit(1)"
        media=s.Media([source('v',720),source('a')],0,9999999999,{})
        with patch.object(s.subprocess,'Popen',side_effect=lambda *a,**kw:real_popen(['python3','-c',script],**kw)):
            session=self.manager.reserve(self.item)
            with self.assertRaises(s.StreamError):session.start_remux(media)
            self.assertTrue(session.auth_failed);session.close()
            self.assertFalse(self.manager.sessions)


if __name__=='__main__':unittest.main()
