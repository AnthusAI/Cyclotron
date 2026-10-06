"""Offline, self-contained playback of private recorded flywheel events."""
import argparse
import json
from pathlib import Path
import sqlite3
from .trace_timeline import timeline_data
from .trace_rounds import round_details


def read_trace(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        return [{**json.loads(payload), 'event_id': number}
                for number, payload in db.execute('SELECT id,payload FROM runtime_events ORDER BY id')]


def render_trace(events, reviewer_history=()):
    def encode(value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False)
        return data.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    vendor = Path(__file__).parent / 'vendor' / 'vis-timeline'
    javascript = (vendor / 'standalone/umd/vis-timeline-graph2d.min.js').read_text()
    css = (vendor / 'styles/vis-timeline-graph2d.min.css').read_text()
    return TEMPLATE.replace('__RECORDING__', encode(events)).replace(
        '__PRESENTATION__', encode(recover_configurations(events))).replace(
        '__REVIEW_HISTORY__', encode(reviewer_history)).replace('__EXCHANGES__', encode(exchange_indices(events))).replace('__ROUNDS__', encode(round_details(events))).replace(
        '__TIMELINE_DATA__', encode(timeline_data(events))).replace(
        '__TIMELINE_JS__', javascript.replace('</script', '<\\/script')).replace('__TIMELINE_CSS__', css).replace(
        '__VENDOR_LICENSE__', encode((vendor / 'LICENSE.MIT.txt').read_text()))


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
    parser.add_argument('--reviews', type=Path, help='optional original reviewer source history, read-only')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('choose a new output path; recorded artifacts are not overwritten')
    if args.output.suffix != '.html':
        parser.error('output must be an HTML file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        from .trace_review_history import read_review_history
        stream.write(render_trace(read_trace(args.database), read_review_history(args.reviews) if args.reviews else ()))
    print(f'Private offline trace: {args.output.resolve()}')


TEMPLATE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'">
<title>Decision Flywheel — recorded trace</title>
<style>__TIMELINE_CSS__</style>
<script>__TIMELINE_JS__</script>
<style>
:root{color-scheme:light dark;font-family:system-ui,sans-serif;background:Canvas;color:CanvasText}
body{max-width:1100px;margin:24px auto;padding:0 20px}h1{font-size:1.5rem}h2{font-size:1.1rem}
.controls{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:18px 0}
button,input,select{font:inherit}button{padding:8px 12px}input[type=number]{width:6em}
#seek{width:100%}pre{white-space:pre-wrap;overflow-wrap:anywhere;padding:12px;border:1px solid GrayText}
.panels{display:grid;grid-template-columns:1fr 1fr;gap:16px}@media(max-width:700px){.panels{grid-template-columns:1fr}}
.vis-timeline,.vis-panel,.vis-labelset .vis-label,.vis-time-axis .vis-text{color:CanvasText;border-color:GrayText}
.vis-item{background:ButtonFace;color:ButtonText;border-color:GrayText}.vis-item.vis-selected{background:Highlight;color:HighlightText}
</style>
<h1>Decision Flywheel — recorded trace</h1>
<p>Private recording. Playback makes no model calls and changes no study data. Older records may lack configuration snapshots.</p>
<h2>Feedback and optimization timeline</h2><div id="timeline"></div><p id="timeline-note"></p>
<p id="run-bounds"></p><div class="controls"><button id="show-run">This optimization run</button><button id="show-history">Recorded review history</button></div>
<div class="controls"><label>Label <select id="label-filter"><option value="">All labels</option></select></label>
<label>Partition <select id="role-filter"><option value="">All partitions</option></select></label>
<label><input type="checkbox" id="comment-filter"> Only labels with comments</label></div>
<div class="controls"><button id="back">Previous</button><button id="next">Next</button><button id="play">Play</button>
<label>From event <input id="start" type="number" min="1" value="1"></label>
<label>Through event <input id="end" type="number" min="1"></label>
<label>Round <select id="round"><option value="">Select a recorded step</option></select></label></div>
<label for="seek">Recorded event position</label><input id="seek" type="range" min="0" value="0">
<p id="status" aria-live="polite"></p>
<section><h2 id="event-title">Select a timeline event</h2><p id="event-summary"></p><dl id="event-fields"></dl>
<button id="paired-request" hidden>Inspect matching optimizer request</button>
<details id="content-box"><summary id="content-title">Inspect event content</summary><pre id="event-content"></pre></details>
<details><summary>Configuration at this point</summary><pre id="configuration"></pre></details>
<details><summary>Exact raw event</summary><pre id="raw-event"></pre></details></section>
<script id="recording" type="application/json">__RECORDING__</script>
<script id="presentation" type="application/json">__PRESENTATION__</script>
<script id="exchanges" type="application/json">__EXCHANGES__</script>
<script id="round-data" type="application/json">__ROUNDS__</script>
<script id="timeline-data" type="application/json">__TIMELINE_DATA__</script>
<script id="review-history" type="application/json">__REVIEW_HISTORY__</script>
<details><summary>Bundled vis-timeline MIT license</summary><pre id="vendor-license"></pre></details>
<script id="vendor-license-data" type="application/json">__VENDOR_LICENSE__</script>
<script>
const events=JSON.parse(document.getElementById('recording').textContent);
const presentation=JSON.parse(document.getElementById('presentation').textContent);
const exchanges=JSON.parse(document.getElementById('exchanges').textContent);
const roundData=JSON.parse(document.getElementById('round-data').textContent);
const reviewHistory=JSON.parse(document.getElementById('review-history').textContent);
const el=id=>document.getElementById(id);let position=0,timer=null;
el('vendor-license').textContent=JSON.parse(el('vendor-license-data').textContent);
const timelineData=JSON.parse(el('timeline-data').textContent);
const timelineItems=timelineData.items.map(item=>({...item,content:(()=>{const label=document.createElement('span');label.textContent=item.content;return label;})()}));
for(const [index,source] of reviewHistory.entries()){
 const row=source.record,isVote=source.source_table==='review_events';
 const label=isVote?`${row.action}: ${row.label||'—'}${row.comment?' · comment':''}`:`Predicted ${row.predicted_label} (${Math.round(row.confidence*100)}%)`;
 const span=document.createElement('span');span.textContent=label;
 timelineItems.push({id:'source:'+index,source_index:index,group:isVote?'feedback':'decisions',start:row.shown_at||row.created_at,type:'point',content:span});
}
for(const group of [{id:'feedback',content:'Human labels'},{id:'decisions',content:'Decisions'}])if(timelineItems.some(i=>i.group===group.id)&&!timelineData.groups.some(g=>g.id===group.id))timelineData.groups.push(group);
const times=timelineItems.map(i=>Date.parse(i.start)).filter(Number.isFinite),runTimes=events.map(e=>Date.parse(e.created_at)).filter(Number.isFinite);
const minimum=times.length?Math.min(...times):Date.now(),maximum=times.length?Math.max(...times):minimum+1000,padding=Math.max(1000,(maximum-minimum)*.03);
const timeline=new vis.Timeline(el('timeline'),timelineItems,timelineData.groups,{editable:false,showCurrentTime:false,stack:true,orientation:'top',min:minimum-padding,max:maximum+padding,zoomMin:1000,zoomMax:Math.max(2000,maximum-minimum+2*padding)});
function showRun(){if(runTimes.length)timeline.setWindow(Math.min(...runTimes)-1000,Math.max(...runTimes)+1000);}
el('show-run').onclick=showRun;el('show-history').onclick=()=>timeline.fit();
showRun();
el('run-bounds').textContent=runTimes.length?`Optimization run: ${new Date(Math.min(...runTimes)).toLocaleString()} → ${new Date(Math.max(...runTimes)).toLocaleString()}. Review history is original source data, not a replay of this run.`:'No timestamped run';
for(const [id,values] of [['label-filter',[...events.filter(e=>e.kind==='human-feedback').map(e=>String(e.feedback?.final_answer_value??'unlabeled')),...reviewHistory.filter(s=>s.source_table==='review_events'&&s.record.label).map(s=>s.record.label)]],
 ['role-filter',[...events.filter(e=>e.kind==='human-feedback').map(e=>e.assignment||'unassigned'),...reviewHistory.map(s=>s.article.assignment)]]]){
 for(const value of [...new Set(values)].sort()){const option=document.createElement('option');option.value=value;option.textContent=value;el(id).append(option);}
}
function applyFilters(){
 timeline.setItems(timelineItems.filter(item=>{
 if(item.source_index!==undefined){const source=reviewHistory[item.source_index];if(source.source_table!=='review_events')return true;
  return (!el('label-filter').value||source.record.label===el('label-filter').value)&&(!el('role-filter').value||source.article.assignment===el('role-filter').value)&&(!el('comment-filter').checked||Boolean(source.record.comment));}
 const e=events[item.event_index];if(e.kind!=='human-feedback')return true;
  return (!el('label-filter').value||String(e.feedback?.final_answer_value??'unlabeled')===el('label-filter').value)
   &&(!el('role-filter').value||(e.assignment||'unassigned')===el('role-filter').value)
   &&(!el('comment-filter').checked||Boolean(e.feedback?.edit_comment_value));}));
}
for(const id of ['label-filter','role-filter','comment-filter'])el(id).onchange=applyFilters;
timeline.on('select',properties=>{if(properties.items.length){const id=properties.items[0];if(String(id).startsWith('source:'))inspectSource(Number(String(id).split(':')[1]));else move(Number(id));}});
el('timeline-note').textContent=`${events.filter(e=>e.kind==='human-feedback').length} flywheel feedback events; ${reviewHistory.filter(s=>s.source_table==='review_events').length} original human actions and ${reviewHistory.filter(s=>s.source_table==='presentations').length} original pre-vote predictions. Pan/zoom and click individual markers. ${timelineData.undated_count} events lack valid timestamps. vis-timeline 8.5.4 (MIT).`;
let cursorAdded=false;
const pretty=value=>JSON.stringify(value,null,2);
// Display-only decoding: raw events below are kept byte-for-byte semantically intact.
function readable(value,depth=0){
 const pad='  '.repeat(depth);
 if(typeof value==='string'){
  const trimmed=value.trim();
  if(trimmed.startsWith('{')||trimmed.startsWith('[')){
   try{return readable(JSON.parse(value),depth);}catch(error){}
  }
  return value;
 }
 if(value===null||typeof value!=='object')return String(value);
 const entries=Array.isArray(value)?value.map((v,i)=>[`[${i+1}]`,v]):Object.entries(value);
 if(!entries.length)return Array.isArray(value)?'[]':'{}';
 return entries.map(([key,child])=>`${pad}${key}:\n${readable(child,depth+1).split('\n').map(line=>'  '+line).join('\n')}`).join('\n\n');
}
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
 if(event?.created_at&&!Number.isNaN(Date.parse(event.created_at))){
  if(!cursorAdded){timeline.addCustomTime(event.created_at,'playback');cursorAdded=true;}
  else timeline.setCustomTime(event.created_at,'playback');
 }
 el('configuration').textContent=readable(snapshots[position]||'Configuration not captured at this point');
 el('raw-event').textContent=event?pretty(event):'No recorded events';
 inspectEvent(event);
 el('back').disabled=!events.length||position===0;el('next').disabled=!events.length||position===events.length-1;el('play').disabled=!events.length;
}
function inspectEvent(event){
 el('event-fields').replaceChildren();
 el('content-box').open=false;
 el('paired-request').hidden=true;
 if(!event){el('event-title').textContent='No recorded events';return;}
 el('event-title').textContent=event.kind.replaceAll('-',' ');
 el('event-summary').textContent=`Event ${event.event_id} · ${event.created_at||'time unavailable'} · ${event.step_stage||'unscoped'}`;
 const field=(label,value)=>{if(value===undefined||value===null)return;const term=document.createElement('dt'),description=document.createElement('dd');term.textContent=label;description.textContent=typeof value==='object'?readable(value):String(value);el('event-fields').append(term,description);};
 let payload=event,title='Full event content';
 if(event.kind==='human-feedback'){
  field('Label',event.feedback?.final_answer_value);field('Partition',event.assignment);field('Action',event.action);
  field('Explanation',event.feedback?.edit_comment_value);field('Item',event.feedback?.item_id);
 }else if(event.kind==='optimizer-request'){
  field('Model',event.requested_model);field('Messages',event.messages?.length);
  try{const briefing=JSON.parse(event.messages.at(-1).content);field('Feedback items',briefing.feedback?.length);field('Human explanations',briefing.human_explanations);field('Control being optimized',briefing.current?.control_under_test);
   for(const id of briefing.current?.example_ids||[]){const example=briefing.feedback?.find(row=>row.id===id);field('Selected example '+id,example?{label:example.label,content:example.values,explanation:example.comment}:'Not present in this recorded context');}
  }catch(error){}
  payload=event.messages;title='Read exact optimizer messages';
  el('content-box').open=true;
 }else if(event.kind==='optimizer-response'){
  field('Model',event.model);let proposal;
  try{proposal=JSON.parse(event.content);}catch(error){}
  field('Rationale',proposal?.rationale);
  const config=snapshots[position]?.config||snapshots[position]?.configuration||{};
  if(proposal)for(const key of ['rubric','example_ids','tasks','dynamic_elements'])if(key in proposal){field('Before: '+key,config[key]??'Unavailable');field('Proposed: '+key,proposal[key]);}
  field('Tool calls returned',event.tool_calls?.length||0);field('Status','Proposal only — not an active configuration change');
  payload={content:proposal||event.content,tool_calls:event.tool_calls||[],usage:event.usage};title='Read optimizer response and returned tool calls';
  const owner=roundData.owners[String(position)],round=roundData.rounds[String(owner)];
  const request=round?.optimizer_requests.filter(i=>i<position&&(!event.briefing_fingerprint||events[i].briefing_fingerprint===event.briefing_fingerprint)).at(-1);
  if(request!==undefined){el('paired-request').hidden=false;el('paired-request').onclick=()=>move(request);}
 }else if(event.kind==='candidate-evaluated'){
  for(const key of ['accuracy','balanced_accuracy','balanced_brier']){field('Incumbent '+key,event.incumbent?.[key]);field('Candidate '+key,event.candidate?.[key]);}
  field('Reason',event.reason);field('Independent evaluation',event.evaluation_independent_of_optimizer_context);
 }else if(event.kind==='decision-request'){
  field('Model',event.model);field('Target',event.target_id);field('Examples',event.state?.examples?.length);field('Questions',Object.keys(event.questions||{}).join(', '));title='Inspect actual expanded decision request';
  for(const [index,example] of (event.state?.examples||[]).entries())field('Example '+(index+1),example);
 }else if(event.kind==='decision-response'){
  for(const [name,answer] of Object.entries(event.answers||{}))field(name,answer);
 }else{
  field('Reason',event.reason||event.result?.reason);field('Status',event.status);field('Training items',event.training_count);field('Feature count',event.features?.length);
  field('Promoted',event.promoted??event.result?.promoted);
 }
 el('content-title').textContent=title;el('event-content').textContent=readable(payload);
}
function inspectSource(index){
 const source=reviewHistory[index],row=source.record;
 el('event-fields').replaceChildren();el('paired-request').hidden=true;el('content-box').open=false;
 el('event-title').textContent=source.source_table==='presentations'?`Predicted ${row.predicted_label} · ${Math.round(row.confidence*100)}%`:`Human ${row.action}: ${row.label||'—'}`;
 el('event-summary').textContent=`Original reviewer record · ${row.shown_at||row.created_at} · ${source.article.assignment}. Not generated by this optimization run.`;
 for(const [label,value] of Object.entries({Title:source.article.title,Abstract:source.article.abstract,Explanation:row.comment,'Prediction shown before vote':source.presentation?`${source.presentation.predicted_label} (${Math.round(source.presentation.confidence*100)}%)`:undefined}))if(value){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;el('event-fields').append(dt,dd);}
 el('event-content').textContent=readable(source);el('raw-event').textContent=pretty(row);el('configuration').textContent='Historical source record: configuration not reconstructed from later optimization state.';
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
