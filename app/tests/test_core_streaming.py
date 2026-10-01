import datetime
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import core


class CoreRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.old=os.getcwd();os.chdir(self.tmp.name)
        Path('outputs').mkdir()
        self.core=core.yt_modoki2.__new__(core.yt_modoki2)
        self.core.config=core.yt_modoki2.settings(self.core)
        self.core.video_dic={};self.core.queue_list=[];self.core.log=lambda *a:None
    def tearDown(self):os.chdir(self.old);self.tmp.cleanup()
    def test_direct_request_is_lazy_and_deduplicated(self):
        with patch.object(core,'YoutubeDL') as ydl:
            uuid,exists=self.core.new_request('https://youtube.com/watch?v=test',True)
            self.assertFalse(exists);self.assertEqual(self.core.video_dic[uuid].status,'completed')
            self.assertEqual(self.core.queue_list,[]);ydl.assert_not_called()
            uuid2,exists=self.core.new_request('https://youtube.com/watch?v=test',True)
            self.assertEqual(uuid,uuid2);self.assertTrue(exists)
    def test_saved_download_keeps_format_and_output(self):
        uuid,_=self.core.new_request('https://youtube.com/watch?v=test',False)
        test=self
        class YDL:
            def __init__(self,options):test.options=options
            def __enter__(self):return self
            def __exit__(self,*a):pass
            def extract_info(self,url):
                Path(f'outputs/{uuid}/output.mp4').write_bytes(b'fixture')
                return {'title':'saved','upload_date':'20260913'}
        downloader=core.yt_modoki2.downloader(self.core)
        with patch.object(core,'YoutubeDL',YDL),patch.object(core,'sleep',side_effect=StopIteration):
            with self.assertRaises(StopIteration):downloader.ytdlp_download(0)
        self.assertEqual(self.core.video_dic[uuid].status,'completed')
        self.assertTrue(Path(f'outputs/{uuid}/output.mp4').exists())
        self.assertEqual(self.options['merge_output_format'],'mp4')
        self.assertIn('bv[vcodec',self.options['format'])
        self.assertEqual(self.options['extractor_args']['youtubepot-bgutilhttp']['base_url'],[self.core.config.download['pot_provider']])


class GunicornLifecycleTests(unittest.TestCase):
    def test_hook_configuration_and_cleanup(self):
        import runpy
        import sys
        from unittest.mock import Mock
        from gunicorn.config import Config
        hooks=runpy.run_path(str(Path(core.__file__).with_name('gunicorn.conf.py')))
        config=Config()
        for name in ('worker_exit','worker_int','worker_abort'):
            config.set(name,hooks[name])  # Gunicorn validates the required callback arity.
        streams=SimpleNamespace(close=Mock())
        with patch.dict(sys.modules,{'frontend':SimpleNamespace(system=SimpleNamespace(streams=streams))}):
            config.worker_exit(None,None)
            config.worker_int(None)
            config.worker_abort(None)
        self.assertEqual(streams.close.call_count,3)
