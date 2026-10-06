"""Offline, self-contained playback of private recorded flywheel events."""
import argparse
import json
from pathlib import Path
import sqlite3


def read_trace(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        return [{**json.loads(payload), 'event_id': number}
                for number, payload in db.execute('SELECT id,payload FROM runtime_events ORDER BY id')]


def render_trace(events):
    def encode(value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False)
        return data.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return TEMPLATE.replace('__RECORDING__', encode(events)).replace(
        '__PRESENTATION__', encode(recover_configurations(events))).replace(
        '__EXCHANGES__', encode(exchange_indices(events)))


def exchange_indices(events):
    latest = dict.fromkeys(('optimizer_request', 'optimizer_response', 'decision_request', 'decision_response'))
    positions = []
    for index, event in enumerate(events):
        name = event.get('kind', '').replace('-', '_')
        if name in latest:
            latest[name] = index
            if name.endswith('_request'):
                latest[name.replace('_request', '_response')] = None
        positions.append(dict(latest))
    return positions


def exchanges_at(events, position):
    return {name: events[index] if index is not None else None
            for name, index in exchange_indices(events)[position].items()}


def recover_configurations(events):
    """Recover decision context only from the same round's recorded request."""
    result = {}
    boundaries = {'step-started', 'round-started', 'optimization-stage-started'}
    for index, event in enumerate(events):
        if event.get('classifier_snapshot') or event.get('kind') not in boundaries:
            continue
        for candidate in events[index+1:]:
            if candidate.get('kind') in {'step-started', 'round-started'}:
                break
            if candidate.get('kind') != 'optimizer-request':
                continue
            try:
                briefing = json.loads(candidate['messages'][-1]['content'])
                current = briefing['current']
            except (KeyError, IndexError, TypeError, ValueError):
                break
            result[index] = {
                'configuration': current, 'main_task': briefing.get('task'),
                'source_event_id': candidate.get('event_id'),
                'provenance': 'Decision context recovered from this round’s actual optimizer request',
                'learned_head': 'Not present in this request; not reconstructed'}
            break
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('choose a new output path; recorded artifacts are not overwritten')
    if args.output.suffix != '.html':
        parser.error('output must be an HTML file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        stream.write(render_trace(read_trace(args.database)))
    print(f'Private offline trace: {args.output.resolve()}')


TEMPLATE = '''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>Decision Flywheel — recorded trace</title>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif;background:Canvas;color:CanvasText}
body{max-width:1100px;margin:24px auto;padding:0 20px}h1{font-size:1.5rem}h2{font-size:1.1rem}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:18px 0}
button,input,select{font:inherit}button{padding:8px 12px}input[type=number]{width:6em}
#seek{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;border:1px solid GrayText}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:700px){.panels{grid-template-columns:1fr}}
</style>
<h1>Decision Flywheel — recorded trace</h1>
<p>Private recording. Playback makes no model calls and changes no study data. Older records may lack configuration snapshots.</p>
<div class="controls"><button id="back">Previous</button><button id="next">Next</button><button id="play">Play</button>
<label>From event <input id="start" type="number" min="1" value="1"></label>
<label>Through event <input id="end" type="number" min="1"></label>
<label>Round <select id="round"><option value="">Select a recorded step</option></select></label></div>
<label for="seek">Recorded event position</label><input id="seek" type="range" min="0" value="0">
<p id="status" aria-live="polite"></p>
<div class="panels"><section><h2>Active configuration at this point</h2><pre id="configuration"></pre></section>
<section><h2>Measured comparison at this event</h2><pre id="metrics"></pre></section></div>
<section><h2>Latest optimizer request at this point — exact system/user messages</h2><pre id="optimizer_request"></pre></section>
<section><h2>Latest optimizer response — actual content and tool calls</h2><pre id="optimizer_response"></pre></section>
<section><h2>Latest decision-model request — actual expanded state and questions</h2><pre id="decision_request"></pre></section>
<section><h2>Latest decision-model response</h2><pre id="decision_response"></pre></section>
<section><h2>Exact recorded event: prompts, responses, requests, fits and results</h2><pre id="detail"></pre></section>
<script id="recording" type="application/json">__RECORDING__</script>
<script id="presentation" type="application/json">__PRESENTATION__</script>
<script id="exchanges" type="application/json">__EXCHANGES__</script>
<script>
const events=JSON.parse(document.getElementById('recording').textContent);
const presentation=JSON.parse(document.getElementById('presentation').textContent);
const exchanges=JSON.parse(document.getElementById('exchanges').textContent);
const el=id=>document.getElementById(id);let position=0,timer=null;
const pretty=value=>JSON.stringify(value,null,2);
const snapshots=[];let config=null;
events.forEach((event,index)=>{
 if(event.kind==='step-started'||(event.kind==='round-started'&&!event.step_id))config=null;
 if(event.classifier_snapshot) config=event.classifier_snapshot;
 else if(presentation[index]) config=presentation[index];
 else if(event.kind==='step-started' && event.configuration) config={configuration:event.configuration,classifier_version:event.classifier_version,head:'Not captured in this older event'};
 snapshots.push(config);
 if(event.kind==='step-started'||event.kind==='round-started') {const option=document.createElement('option');option.value=index;option.textContent=`${event.event_id}: ${event.step_stage||'optimization'} · ${event.trigger||'recorded round'}`;el('round').append(option);}
});
const latestStep=events.map(e=>e.kind).lastIndexOf('step-started');
position=Math.max(0,events.length-1);el('start').value=Math.max(0,latestStep)+1;
el('round').value=String(Math.max(0,latestStep));
el('seek').max=Math.max(0,events.length-1);el('end').value=events.length;
function stop(){if(timer!==null)clearInterval(timer);timer=null;el('play').textContent='Play';}
function comparison(event){
 const result=event.result||event;
 const rows=result.trials||(result.incumbent&&result.candidate?[result]:[]);
 if(!rows.length)return 'No measured comparison at this event. No accuracy change established.';
 return pretty(rows.map(row=>({feature_set:row.feature_set,training_class_weighting:row.training_class_weighting,
  incumbent:row.incumbent,candidate:row.candidate,
  accuracy_delta:row.candidate&&row.incumbent?row.candidate.accuracy-row.incumbent.accuracy:null,
  balanced_accuracy_delta:row.candidate&&row.incumbent?row.candidate.balanced_accuracy-row.incumbent.balanced_accuracy:null,
  promoted:row.promoted,reason:row.reason,evaluation_independent_of_optimizer_context:row.evaluation_independent_of_optimizer_context})));
}
function draw(){
 const event=events[position];
 el('status').textContent=event?`Event ${position+1}/${events.length} · ID ${event.event_id} · ${event.kind} · ${event.step_stage||'legacy/unscoped'} · ${event.status||event.reason||''}`:'No recorded events';
 el('seek').value=position;
 el('configuration').textContent=pretty(snapshots[position]||'Configuration not captured at this point');
 el('metrics').textContent=event?comparison(event):'No measured comparison at this event';
 el('detail').textContent=event?pretty(event):'No recorded events';
 for(const name of ['optimizer_request','optimizer_response','decision_request','decision_response']){
  const index=exchanges[position]?.[name];
  el(name).textContent=index!==null&&index!==undefined?pretty(events[index]):'No matching exchange recorded by this point';
 }
 el('back').disabled=!events.length||position===0;el('next').disabled=!events.length||position===events.length-1;el('play').disabled=!events.length;
}
function move(index){stop();position=Math.max(0,Math.min(events.length-1,index));draw();}
el('back').onclick=()=>move(position-1);el('next').onclick=()=>move(position+1);
el('seek').oninput=()=>move(Number(el('seek').value));el('round').onchange=()=>{if(el('round').value!=='')move(Number(el('round').value));};
el('play').onclick=()=>{
 if(timer!==null){stop();return;}
 const first=Number(el('start').value)-1,last=Number(el('end').value)-1;
 if(!Number.isInteger(first)||!Number.isInteger(last)||first<0||last>=events.length||first>last){el('status').textContent='Choose a valid event range';return;}
 if(position<first||position>=last)position=first;draw();el('play').textContent='Pause';
 timer=setInterval(()=>{if(position>=last){stop();return;}position++;draw();},1000);
};draw();
</script></html>'''


if __name__ == '__main__':
    main()
