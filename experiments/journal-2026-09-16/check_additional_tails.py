import json,hashlib,subprocess,xml.etree.ElementTree as E
from pathlib import Path
from urllib.parse import urlsplit
import httpx
from src.config import load_settings
from faster_whisper import WhisperModel
p=Path('data/journal-investigation-2026-09-16');s=load_settings();rows=json.loads((p/'database-episodes.json').read_text());items=E.parse(p/'feed.xml').findall('channel/item');m=WhisperModel('base',device='cpu',compute_type='int8',local_files_only=True)
for id in [8226097,8226093,8226089]:
 row=next(r for r in rows if r['id']==id);item=next(i for i in items if i.findtext('title').lstrip('| ')==row['title']);d=p/str(id);d.mkdir(exist_ok=True);audio=d/'served.mp3'
 if not audio.exists():
  url=item.find('enclosure').get('url');r=httpx.get(s.worker.server_url+urlsplit(url).path,timeout=120);r.raise_for_status();audio.write_bytes(r.content)
 duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','default=noprint_wrappers=1:nokey=1',str(audio)]));start=max(0,duration-150);clip=d/'tail.wav';subprocess.run(['ffmpeg','-v','error','-y','-ss',str(start),'-i',str(audio),'-t','150','-ar','16000','-ac','1',str(clip)],check=True)
 meta={'id':id,'title':row['title'],'duration':duration,'start':start,'sha256':hashlib.sha256(audio.read_bytes()).hexdigest(),'model':'base','device':'cpu','compute_type':'int8','language':'auto','condition_on_previous_text':False,'vad_filter':True,'word_timestamps':True,'local_files_only':True};(d/'tail-metadata.json').write_text(json.dumps(meta,indent=2));ss,info=m.transcribe(str(clip),word_timestamps=True,condition_on_previous_text=False,vad_filter=True);out=[]
 for seg in ss:
  out.append({'start':start+seg.start,'end':start+seg.end,'text':seg.text});print(id,round(start+seg.start,2),seg.text,flush=True)
 (d/'tail-transcript.json').write_text(json.dumps(out,indent=2));print('DONE',id,flush=True)
