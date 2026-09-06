import json, subprocess, wave, time, sys, os
from pathlib import Path
import numpy as np
sys.path.insert(0,str(Path.cwd()))
from src.pipeline.editor import _speech_intervals, _resolve_cut_ranges
from faster_whisper import WhisperModel
import stable_whisper
ROOT=Path(os.environ.get('EXPERIMENT_DIR','data/slicing-experiment'))
ROOT.mkdir(parents=True,exist_ok=True)
def run(*args): subprocess.run(args,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
texts=['The shipping container changed how goods travel around the world.', 'This episode is sponsored by Squarespace. Visit our website for a discount today.', 'Welcome back. Now let us return to the history of international shipping.']
chunks=[]
for i,text in enumerate(texts):
    p=ROOT/f'part{i}'
    run('say','-v',os.environ.get('VOICE','Samantha'),'-r',os.environ.get('RATE','175'),'-o',str(p)+'.aiff',text)
    run('ffmpeg','-y','-i',str(p)+'.aiff','-ar','16000','-ac','1',str(p)+'.wav')
    with wave.open(str(p)+'.wav') as f: a=np.frombuffer(f.readframes(f.getnframes()),dtype=np.int16)
    nz=np.flatnonzero(np.abs(a.astype(float))>100)
    chunks.append(a[max(0,nz[0]-160):min(len(a),nz[-1]+161)])
base=WhisperModel('base',device='cpu',compute_type='int8')
stable=stable_whisper.load_faster_whisper('base',device='cpu',compute_type='int8')
rows=[]
for gap in [0.0,0.1,0.3]:
    silence=np.zeros(round(gap*16000),dtype=np.int16)
    a=np.concatenate([chunks[0],silence,chunks[1],silence,chunks[2]])
    path=ROOT/f'gap-{gap}.wav'
    with wave.open(str(path),'wb') as f: f.setparams((1,2,16000,0,'NONE','')); f.writeframes(a.tobytes())
    true_start=(len(chunks[0])+len(silence))/16000
    true_end=true_start+len(chunks[1])/16000
    previous_end=len(chunks[0])/16000
    next_start=true_end+gap
    for method in ['baseline','stable','refine']:
        t=time.monotonic()
        if method=='baseline':
            segs,_=base.transcribe(str(path),word_timestamps=True)
            segs=[{'start':s.start,'end':s.end,'text':s.text,'words':[{'start':w.start,'end':w.end,'word':w.word} for w in s.words]} for s in segs]
        elif method=='stable':
            result=stable.transcribe(str(path),verbose=False)
            segs=result.to_dict()['segments']
        else:
            stable.refine(str(path),result,verbose=False)
            segs=result.to_dict()['segments']
        elapsed=time.monotonic()-t
        (ROOT/f'{gap}-{method}.json').write_text(json.dumps(segs,indent=2))
        words=[w for s in segs for w in s['words']]
        start=next(w['start'] for w in words if w['word'].strip().lower()=='this')
        end=next(w['end'] for w in words if w['word'].strip().lower().rstrip('.!,')=='today')
        # Known lexical selection, isolate aligner and editor from classifier.
        for selection,bs,be in [('exact_audio',true_start,true_end),('word_selection',start,end)]:
            cut=_resolve_cut_ranges([{'start':bs,'end':be}],_speech_intervals(segs),len(a)/16000,.5)[0]
            rows.append(dict(gap=gap,method=method,selection=selection,seconds=round(elapsed,2),boundary_error_ms=[round((start-true_start)*1000),round((end-true_end)*1000)],cut=cut,content_clipped_ms=round((max(0,previous_end-cut[0])+max(0,cut[1]-next_start))*1000),ad_retained_ms=round((max(0,cut[0]-true_start)+max(0,true_end-cut[1]))*1000)))
        print(json.dumps(rows[-2:]),flush=True)
(ROOT/'results.json').write_text(json.dumps(rows,indent=2))
