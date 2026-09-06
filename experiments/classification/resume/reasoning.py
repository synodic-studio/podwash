import json,time,sys,concurrent.futures,hashlib
from pathlib import Path
import requests
sys.path.insert(0,str(Path.cwd()))
from experiments.classification.evaluate import load_cases,build_prompt,score,request_body
root=Path('data/classification-eval/resume');cases=load_cases()
def run(name):
 case=cases[name];prompt=build_prompt(case,'words-v2');body=request_body('dsf',prompt)
 body.update(thinking={'type':'enabled'},extra_body={'thinking':{'type':'enabled'}},reasoning_effort='low')
 t=time.monotonic();r=requests.post('http://localhost:4000/v1/chat/completions',json=body,timeout=240);r.raise_for_status();data=r.json();choice=data['choices'][0];text=choice['message'].get('content') or ''
 row=dict(case=name,model='dsf',policy='words-v2',reasoning_effort='low',seconds=round(time.monotonic()-t,2),cost_usd=float(r.headers.get('x-litellm-response-cost',0)),usage=data.get('usage'),response_id=data.get('id'),prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest(),output_text=text)
 try:
  assert choice['finish_reason']=='stop',choice['finish_reason']
  parsed=json.loads(text[text.index('{'):text.rindex('}')+1]);row['parsed']=parsed;row['score']=score(case,parsed,'words')
 except Exception as e:row['error']=str(e)
 (root/f'low-{name}.json').write_text(json.dumps(row,indent=2));print(json.dumps({k:row[k] for k in ['case','seconds','cost_usd','score','error'] if k in row}),flush=True);return row
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as p:
 rows=list(p.map(run,[n for n in cases if n!='ologies_full']))
(root/'low-results.json').write_text(json.dumps(rows,indent=2))
