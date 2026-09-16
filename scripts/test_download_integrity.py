import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent))
import download_my_recordings as dl
import verify_meeting_dir as verify
class FakeResponse:
 def __init__(self,status,headers=None,chunks=(b'x',)): self.status=status; self.headers=headers or {}; self.chunks=iter(chunks)
 def __enter__(self): return self
 def __exit__(self,*a): return False
 def read(self,_n): return next(self.chunks,b'')
class DownloadIntegrityTests(unittest.TestCase):
 def test_partial_existing_file_is_not_skipped(self):
  with tempfile.TemporaryDirectory() as td:
   dest=Path(td)/'x.mp4'; dest.write_bytes(b'x'*96)
   with patch.object(dl,'urlopen',side_effect=RuntimeError('network')): self.assertTrue(dl.download_file('https://example.invalid/x',dest,100).startswith('error:'))
 def test_range_must_start_at_requested_offset(self):
  with tempfile.TemporaryDirectory() as td:
   dest=Path(td)/'x.mp4'; dest.with_suffix('.mp4.part').write_bytes(b'x'*4)
   with patch.object(dl,'urlopen',return_value=FakeResponse(206,{'Content-Range':'bytes 0-9/10'},(b'0123456789',))): self.assertTrue(dl.download_file('https://example.invalid/x',dest,10).startswith('error:'))
 def test_manifest_requires_media_and_accepts_filename(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); (p/'x.mp4').write_bytes(b'x'); (p/'manifest.json').write_text(json.dumps({'files':[{'filename':'x.mp4','size':1}]}))
   with patch.object(verify,'ffprobe',return_value={'streams':[{'codec_type':'video'}],'format':{'duration':'1'}}): self.assertTrue(verify.verify(p))
 def test_empty_manifest_fails(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); (p/'manifest.json').write_text(json.dumps({'files':[]})); self.assertFalse(verify.verify(p))
 def test_text_artifacts_are_not_sent_to_ffprobe(self):
  with tempfile.TemporaryDirectory() as td:
   p=Path(td); (p/'x.mp4').write_bytes(b'x'); (p/'转写.txt').write_text('text')
   (p/'manifest.json').write_text(json.dumps({'files':[{'filename':'x.mp4','size':1},{'filename':'转写.txt','size':4}]}))
   with patch.object(verify,'ffprobe',return_value={'streams':[{'codec_type':'video'}],'format':{'duration':'1'}}) as probe:
    self.assertTrue(verify.verify(p)); probe.assert_called_once()
