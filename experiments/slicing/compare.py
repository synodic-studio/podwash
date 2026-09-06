import json, wave, subprocess, os
import numpy as np
from pathlib import Path
root=Path(os.environ.get('EXPERIMENT_DIR','data/slicing-experiment'))
rows=json.loads((root/'results.json').read_text())
for row in rows:
 if row['selection']!='word_selection': continue
 gap=row['gap']; method=row['method']
 words=[w for s in json.loads((root/f'{gap}-{method}.json').read_text()) for w in s['words']]
 first=next(i for i,w in enumerate(words) if w['word'].strip().lower()=='this')
 last=next(i for i,w in enumerate(words) if w['word'].strip().lower().rstrip('.!,')=='today')
 # Prototype: select word indices, cut halfway through adjacent timestamp gaps.
 start=(words[first-1]['end']+words[first]['start'])/2
 end=(words[last]['end']+words[last+1]['start'])/2
 lengths=[]
 for i in [0,1]:
  with wave.open(str(root/f'part{i}.wav')) as f: a=np.frombuffer(f.readframes(f.getnframes()),dtype=np.int16)
  nz=np.flatnonzero(np.abs(a.astype(float))>100)
  lengths.append((min(len(a),nz[-1]+161)-max(0,nz[0]-160))/16000)
 truth_start=lengths[0]+gap
 truth_end=truth_start+lengths[1]
 row['prototype_cut']=[start,end]
 row['prototype_content_clipped_ms']=round((max(0,truth_start-gap-start)+max(0,end-truth_end-gap))*1000)
 row['prototype_ad_retained_ms']=round((max(0,start-truth_start)+max(0,truth_end-end))*1000)
 for name,(s,e) in [('current',row['cut']),('prototype',(start,end))]:
  subprocess.run(['ffmpeg','-v','error','-y','-i',str(root/f'gap-{gap}.wav'),'-filter_complex',f'[0:a]atrim=end={s},asetpts=PTS-STARTPTS[a];[0:a]atrim=start={e},asetpts=PTS-STARTPTS[b];[a][b]concat=n=2:v=0:a=1[out]','-map','[out]',str(root/f'{gap}-{method}-{name}.wav')],check=True)
 print(json.dumps(row))
(root/'comparison.json').write_text(json.dumps([r for r in rows if r['selection']=='word_selection'],indent=2))
