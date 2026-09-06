import json,time,sys,hashlib
from pathlib import Path
import requests
sys.path.insert(0,str(Path.cwd()))
from experiments.classification.evaluate import load_cases,build_prompt,score,request_body
case=load_cases()['ologies_full']; words=case['words']; selected=set(); rows=[]
root=Path('data/classification-eval/resume')
for start in range(0,len(words),500):
 core=words[start:start+500]; context=words[max(0,start-100):start+600]; valid={w['id'] for w in context}
 sub=dict(case,words=context,segments=[dict(words=context)],remove_ids=[],keep_ids=[])
 prompt=build_prompt(sub,'words-v2'); t=time.monotonic()
 response=requests.post('http://localhost:4000/v1/chat/completions',json=request_body('dsf',prompt),timeout=240); response.raise_for_status(); data=response.json(); choice=data['choices'][0]; output=choice['message']['content']
 row=dict(core=[core[0]['id'],core[-1]['id']],context=[context[0]['id'],context[-1]['id']],response_id=data['id'],output_text=output,seconds=time.monotonic()-t,cost_usd=float(response.headers.get('x-litellm-response-cost',0)),usage=data.get('usage'),prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest())
 try:
  assert choice['finish_reason']=='stop'
  parsed=json.loads(output[output.index('{'):output.rindex('}')+1]); score(sub,parsed,'words');row['parsed']=parsed
  for r in parsed['ranges']:selected.update(set(range(r['first'],r['last']+1))&{w['id'] for w in core})
 except Exception as e:row['error']=str(e)
 rows.append(row);(root/'windows-detail.json').write_text(json.dumps(rows,indent=2));print(json.dumps({k:v for k,v in row.items() if k in ['core','seconds','cost_usd','error','parsed']}),flush=True)
ranges=[]
for i in sorted(selected):
 if ranges and ranges[-1]['last']==i-1:ranges[-1]['last']=i
 else:ranges.append(dict(first=i,last=i))
result=dict(case='ologies_full',model='dsf',policy='words-v2-windows',parsed=dict(ranges=ranges),cost_usd=sum(r['cost_usd'] for r in rows),window_words=500,context_words=100,failed_windows=sum('error' in r for r in rows))
result['score']=score(case,result['parsed'],'words');(root/'windows-result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result),flush=True)
