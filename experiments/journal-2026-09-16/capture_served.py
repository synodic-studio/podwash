"""Capture four currently published Journal episodes and transcribe locally.

Run from repo root with PYTHONPATH=. uv run --extra worker python <this-file>.
Requires episodes.json and feed.xml snapshots in the dated data directory.
No model API calls; cached Whisper base is used (downloads if absent).
Existing transcripts are reused. The manifest and feed must have matching order.
"""
from src.config import load_settings
import httpx,json,xml.etree.ElementTree as E,hashlib
from urllib.parse import urlsplit
from pathlib import Path
from faster_whisper import WhisperModel
s=load_settings();p=Path('data/journal-investigation-2026-09-16'); episodes=json.loads((p/'episodes.json').read_text());items=E.fromstring((p/'feed.xml').read_bytes()).findall('channel/item');model=WhisperModel('base',device='cpu',compute_type='int8')
for ep,item in zip(episodes[:4],items[:4]):
 d=p/str(ep['id']);d.mkdir(exist_ok=True);audio=d/'served.mp3'
 if not audio.exists():
  r=httpx.get(s.worker.server_url+urlsplit(item.find('enclosure').get('url')).path,timeout=120);r.raise_for_status();audio.write_bytes(r.content)
 (d/'metadata.json').write_text(json.dumps({'episode':ep,'sha256':hashlib.sha256(audio.read_bytes()).hexdigest()},indent=2))
 r=httpx.get(s.worker.server_url+f"/api/episodes/{ep['id']}/logs",headers={'Authorization':'Bearer '+s.admin.token},timeout=30);(d/'logs.json').write_text(r.text)
 if (d/'transcript.json').exists():continue
 print('TRANSCRIBING',ep['id'],ep['title'],flush=True);segments,info=model.transcribe(str(audio),word_timestamps=True,condition_on_previous_text=False,vad_filter=True);out=[]
 with (d/'transcript.txt').open('w') as f:
  for x in segments:
   out.append(x._asdict());line=f'{x.start:.2f}-{x.end:.2f} {x.text}';f.write(line+'\n');f.flush()
 (d/'transcript.json').write_text(json.dumps(out,indent=2));print('DONE',ep['id'],info.duration,flush=True)
