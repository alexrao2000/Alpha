"""Offline human review page: anonymized display, JSON export, no network calls."""

from __future__ import annotations

from hashlib import sha256
import json

from .qualitative import _payload, run_key


def render_human_review(report: dict, template: dict) -> str:
    """Render star and A/B surveys for the same holdout tasks as LLM judging."""
    known = {run_key(r): r for r in report["runs"] if r["split"] == "holdout"}
    ratings = []
    for item in template["scores"]:
        run = known[item["key"]]
        ratings.append({
            "case": run["case_id"], "trial": run["trial"],
            "payload": _payload(run),
        })
    pairs = []
    for item in template["pairs"]:
        base = known[item["baseline_key"]]
        changed = known[item["candidate_key"]]
        # Presentation is blind to workflow identity; export maps A/B back to
        # baseline/candidate. Source code necessarily contains the mapping.
        a_is_baseline = sha256(
            (item["baseline_key"] + item["candidate_key"] + ":human").encode()
        ).digest()[0] % 2 == 0
        a, b = (base, changed) if a_is_baseline else (changed, base)
        pairs.append({
            "case": base["case_id"], "trial": base["trial"],
            "option_a": _payload(a), "option_b": _payload(b),
            "a_is_baseline": a_is_baseline,
        })
    serialized = json.dumps(
        {"template": template, "ratings": ratings, "pairs": pairs},
        ensure_ascii=False, allow_nan=False,
    ).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    return """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Alpha · Human evaluation</title>
<style>
:root{font:14px/1.5 system-ui,sans-serif;color:#e6edf8;background:#0d1220}
body{max-width:1050px;margin:0 auto;padding:32px 20px}
h1{font-size:30px}h2{margin-top:32px}
p,.muted{color:#a6b4cd}.muted{font-size:12px}
section{border:1px solid #39445a;background:#171e2d;border-radius:10px;padding:20px;margin:14px 0}
pre{background:#0e1524;padding:12px;max-height:240px;white-space:pre-wrap;overflow:auto}
.options{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}
label{cursor:pointer;padding:7px 12px;border:1px solid #46546e;border-radius:5px}
.columns{display:grid;grid-template-columns:1fr 1fr;gap:12px}
textarea{width:100%;background:#0e1524;color:inherit;border:1px solid #45546c;border-radius:5px;padding:8px}
button{padding:12px 18px;background:#b8f28a;border:0;border-radius:5px;font-weight:bold;cursor:pointer}
@media(max-width:680px){.columns{grid-template-columns:1fr}}
</style></head>
<body>
<h1>Human quality evaluation</h1>
<p>Two surveys: <strong>1–5 stars</strong> for one response, and <strong>A/B</strong> for two responses to the same task. Workflow identities are hidden in this page's visible choices. No answers are sent to a server.</p>
<p class="muted">Your ratings are a reference for assessing whether LLM judges agree with people; they are not objective ground truth.</p>
<section id="grading-guide" aria-label="Shared grading criteria"></section>
<h2>1–5 stars · single-output quality</h2><div id="ratings"></div>
<h2>A/B · relative preference</h2><div id="pairs"></div>
<button id="download" type="button">Download completed human review JSON</button>
<p class="muted">Use the downloaded file with <code>alpha q calibrate --human FILE</code> after <code>alpha q judge</code>. Blank answers stay unrated.</p>
<script type="application/json" id="data">""" + serialized + """</script>
<script>
const data=JSON.parse(document.querySelector('#data').textContent);
const add=(parent,tag,content,cls='')=>{
  const n=document.createElement(tag);if(cls)n.className=cls;
  if(content!==undefined)n.textContent=content;parent.append(n);return n;
};
const payload=thing=>JSON.stringify(thing,null,2);
const rubric=data.template.rubric?.definition;
if(rubric){
  const guide=document.querySelector('#grading-guide');
  add(guide,'h2','Use the same grading rubric as the LLM');
  add(guide,'p',rubric.purpose);
  add(guide,'h3','What to check');
  const criteria=add(guide,'ul');
  rubric.criteria.forEach(item=>add(criteria,'li',item.label+': '+item.description));
  add(guide,'h3','What the 1–5 scores mean');
  const anchors=add(guide,'ul');
  for(let score=1;score<=5;score++)add(anchors,'li',score+' — '+rubric.anchors[String(score)]);
  add(guide,'h3','How to choose A/B');
  add(guide,'p',rubric.pairwise.instruction);
  add(guide,'p',rubric.pairwise.tie_rule);
  add(guide,'p',rubric.limitations,'muted');
  add(guide,'p','Rubric: '+rubric.id+' · version '+rubric.version+' · SHA-256 '+data.template.rubric.sha256.slice(0,12),'muted');
}
const option=(container,group,value,text)=>{
  const label=add(container,'label');const radio=document.createElement('input');
  radio.type='radio';radio.name=group;radio.value=value;label.append(radio,document.createTextNode(' '+text));
};
data.ratings.forEach((entry,i)=>{
  const box=add(document.querySelector('#ratings'),'section');
  add(box,'strong',entry.case+' · trial '+(entry.trial+1));
  add(box,'pre',payload(entry.payload));
  const options=add(box,'div',undefined,'options');
  for(let score=1;score<=5;score++)option(options,'star-'+i,String(score),String(score)+' ★');
  const notes=add(box,'textarea');notes.placeholder='Optional notes';notes.dataset.starNotes=i;
});
data.pairs.forEach((entry,i)=>{
  const box=add(document.querySelector('#pairs'),'section');
  add(box,'strong',entry.case+' · trial '+(entry.trial+1));
  const cols=add(box,'div',undefined,'columns');
  const a=add(cols,'div');add(a,'h3','Option A');add(a,'pre',payload(entry.option_a));
  const b=add(cols,'div');add(b,'h3','Option B');add(b,'pre',payload(entry.option_b));
  const options=add(box,'div',undefined,'options');
  for(const label of ['A','B','tie'])option(options,'pair-'+i,label,label);
  const notes=add(box,'textarea');notes.placeholder='Optional notes';notes.dataset.pairNotes=i;
});
document.querySelector('#download').addEventListener('click',()=>{
  const review=JSON.parse(JSON.stringify(data.template));
  review.scores.forEach((entry,i)=>{
    const selection=document.querySelector('input[name="star-'+i+'"]:checked');
    entry.score=selection?Number(selection.value):null;
    entry.notes=document.querySelector('[data-star-notes="'+i+'"]').value;
  });
  review.pairs.forEach((entry,i)=>{
    const selection=document.querySelector('input[name="pair-'+i+'"]:checked');
    const choice=selection?.value;
    const aIsBaseline=data.pairs[i].a_is_baseline;
    entry.preferred=!choice?null:choice==='tie'?'tie':
      (choice==='A')===aIsBaseline?'baseline':'candidate';
    entry.notes=document.querySelector('[data-pair-notes="'+i+'"]').value;
  });
  const blob=new Blob([JSON.stringify(review,null,2)+'\\n'],{type:'application/json'});
  const url=URL.createObjectURL(blob);
  const anchor=document.createElement('a');anchor.href=url;
  anchor.download='human-review-completed.json';anchor.click();
  setTimeout(()=>URL.revokeObjectURL(url),1000);
});
</script></body></html>
"""
