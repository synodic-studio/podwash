import json,time,sys
from pathlib import Path
import requests
sys.path.insert(0,str(Path.cwd()))
from experiments.classification.evaluate import load_cases,build_prompt,score,request_body
root=Path('data/classification-eval/resume');case=load_cases()['ologies_full']; initial=json.loads((root/'windows-result.json').read_text()); rows=[]; ranges=[]
for candidate in initial['parsed']['ranges']:
 context=[w for w in case['words'] if candidate['first']-100<=w['id']<=candidate['last']+100]
 sub=dict(case,words=context,segments=[dict(words=context)],remove_ids=[],keep_ids=[])
 prompt=build_prompt(sub,'words-v2')+'\n\nREVIEW TASK: An unreliable first pass proposed removing '+json.dumps(candidate)+'. Independently correct this proposal. Return all removable spans in the excerpt. Split a proposal when editorial content separates sponsor mentions; reject false proposals. Topic framing and personal reflections can be editorial even in a sponsored episode. Noncommercial discussion of charitable causes is editorial unless it actually promotes the show or a paid product. Preserve the show introduction, including its title. Inspect the words immediately before and after every returned range so complete promotional sentences are removed without adjacent editorial sentences. Output only the specified JSON.'
 t=time.monotonic();r=requests.post('http://localhost:4000/v1/chat/completions',json=request_body('dsf',prompt),timeout=240);r.raise_for_status();d=r.json();text=d['choices'][0]['message']['content'];p=json.loads(text[text.index('{'):text.rindex('}')+1]);score(sub,p,'words');ranges+=p['ranges'];rows.append(dict(candidate=candidate,parsed=p,cost_usd=float(r.headers.get('x-litellm-response-cost',0)),response_id=d['id'],usage=d.get('usage'),seconds=time.monotonic()-t));print(json.dumps(rows[-1]),flush=True)
result=dict(case='ologies_full',model='dsf',policy='words-v2-windows-review',parsed=dict(ranges=ranges),cost_usd=initial['cost_usd']+sum(r['cost_usd'] for r in rows));result['score']=score(case,result['parsed'],'words');(root/'review-detail.json').write_text(json.dumps(rows,indent=2));(root/'review-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
