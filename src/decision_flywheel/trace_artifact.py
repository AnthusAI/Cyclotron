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
.vis-item.vis-point .vis-dot{width:10px;height:10px;border:0;background:var(--marker-color,GrayText);border-radius:50%;margin-top:-5px;margin-left:-5px}
.vis-item.vis-point .vis-item-content{display:none}
.vis-item.marker-human .vis-dot{border-radius:0;transform:rotate(45deg)}
.vis-item.marker-optimizer-request .vis-dot{background:Canvas;border:2px solid CanvasText;border-radius:0;width:8px;height:8px}
.vis-item.marker-optimizer-response .vis-dot{background:CanvasText;border-radius:0}
.vis-item.vis-selected .vis-dot{outline:3px solid Highlight;outline-offset:3px}
.vis-item.vis-box{background:Canvas;border:1px solid var(--marker-color,GrayText);color:var(--marker-color,CanvasText);min-width:18px;text-align:center}
.vis-item.vis-box .vis-item-content{padding:1px 3px}
.vis-item.vis-line{border-color:var(--marker-color,GrayText)}
.vis-labelset .vis-label{min-height:36px}.vis-group{min-height:36px}
#label-legend{display:flex;gap:16px;flex-wrap:wrap;margin:12px 0}
</style>
<h1>Decision Flywheel — recorded trace</h1>
<p>Private recording. Playback makes no model calls and changes no study data. Older records may lack configuration snapshots.</p>
<h2>Feedback and optimization timeline</h2><div id="timeline"></div><div id="label-legend"></div><p id="timeline-note"></p>
<p id="run-bounds"></p><div class="controls"><button id="show-run">This optimization run</button><button id="show-history">Recorded review history</button></div>
<div class="controls"><label>Label <select id="label-filter"><option value="">All labels</option></select></label>
<label>Partition <select id="role-filter"><option value="">All partitions</option></select></label>
<label><input type="checkbox" id="comment-filter"> Only labels with comments</label></div>
<div class="controls"><button id="back">Previous</button><button id="next">Next</button><button id="play">Play</button>
<button id="zoom-in">Zoom in</button><button id="zoom-out">Zoom out</button>
<label>From event <input id="start" type="number" min="1" value="1"></label>
<label>Through event <input id="end" type="number" min="1"></label>
<label>Round <select id="round"><option value="">Select a recorded step</option></select></label></div>
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
const el=id=>document.getElementById(id);let position=0,timer=null,currentStep=0;
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
// Ordinal positions are a view projection. Original event timestamps stay unchanged.
const ordered=[...events.map((e,index)=>({key:String(index),date:Date.parse(e.created_at)})),...reviewHistory.map((s,index)=>({key:'source:'+index,date:Date.parse(s.record.shown_at||s.record.created_at)}))].sort((a,b)=>(a.date-b.date));
const stepPositions=new Map(ordered.map((record,index)=>[record.key,index*1000]));
const classes=[...new Set([...reviewHistory.map(s=>s.record.label||s.record.predicted_label),...events.filter(e=>e.kind==='human-feedback'||e.kind==='prediction').map(e=>e.label||e.feedback?.final_answer_value)].filter(Boolean))].sort();
const classMark=label=>String.fromCharCode(65+classes.indexOf(label));
const classColor=label=>`hsl(${(classes.indexOf(label)*137+205)%360} 65% 45%)`;
classes.forEach(label=>{const entry=document.createElement('span');entry.textContent=`● ◆ ${label}`;entry.style.color=classColor(label);el('label-legend').append(entry);});
const shapes=document.createElement('span');shapes.textContent='● prediction · ◆ human label · □ optimizer request · ■ optimizer response';el('label-legend').append(shapes);
for(const item of timelineItems){
 item.title=item.content.textContent;item.start=new Date(stepPositions.get(String(item.id)));
 const source=item.source_index!==undefined?reviewHistory[item.source_index]:null,event=events[item.event_index];
 const label=source?(source.record.label||source.record.predicted_label):(event?.label||event?.feedback?.final_answer_value);
 if(label)item.style=`--marker-color:${classColor(label)}`;
 item.className=source?(source.source_table==='presentations'?'marker-prediction':'marker-human'):
  event?.kind==='human-feedback'?'marker-human':event?.kind==='prediction'?'marker-prediction':`marker-${event?.kind||'event'}`;
 if(label&&item.className==='marker-prediction')item.group='prediction-class-'+classes.indexOf(label);
 else if(label&&item.className==='marker-human')item.group='label-class-'+classes.indexOf(label);
 else if(item.group==='decisions')item.group='decision-api';
 item.type='box';
 item.content=document.createElement('span');item.content.textContent=item.className==='marker-human'?'◆':item.className==='marker-prediction'?'●':item.className==='marker-optimizer-request'?'□':item.className==='marker-optimizer-response'?'■':'•';
 item.content.setAttribute('aria-label',item.title);
}
const classGroups=[];
for(const [parent,prefix,title] of [['model-decisions','prediction-class-','Model decisions'],['human-labels','label-class-','Human labels']]){
 const children=[];
 classes.forEach((label,index)=>{const id=prefix+index;if(!timelineItems.some(item=>item.group===id))return;
  const heading=document.createElement('span');heading.textContent=label;heading.style.color=classColor(label);
  children.push({id,content:heading});});
 if(children.length)classGroups.push({id:parent,content:title,nestedGroups:children.map(g=>g.id),showNested:true},...children);
}
const occupied=new Set(timelineItems.map(i=>i.group));
const otherGroups=timelineData.groups.filter(g=>occupied.has(g.id)&&!['decisions'].includes(g.id));
if(occupied.has('decision-api'))otherGroups.push({id:'decision-api',content:'Decision API requests/responses'});
timelineData.groups=[...classGroups,...otherGroups];
const maximum=Math.max(1000,(ordered.length-1)*1000);
const timeline=new vis.Timeline(el('timeline'),timelineItems,timelineData.groups,{editable:false,showCurrentTime:false,stack:false,stackSubgroups:false,orientation:'top',min:-1000,max:maximum+1000,zoomMin:1000,zoomMax:maximum+2000,maxHeight:420,horizontalScroll:false,moveable:true,zoomable:true,preferZoom:true,showMajorLabels:false,format:{minorLabels:date=>`Step ${Math.round(date.valueOf()/1000)+1}`}});
el('zoom-in').onclick=()=>timeline.zoomIn(.5);
el('zoom-out').onclick=()=>timeline.zoomOut(.5);
timeline.on('timechanged',properties=>{if(properties.id==='playback')goStep(Math.round(properties.time.valueOf()/1000));});
function goStep(step){
 stop();const bounded=Math.max(0,Math.min(ordered.length-1,step)),key=ordered[bounded]?.key;
 if(key===undefined)return;
 if(key.startsWith('source:'))inspectSource(Number(key.split(':')[1]));else move(Number(key));
}
function showRun(){const first=stepPositions.get('0')||0;timeline.setWindow(first-1000,Math.min(maximum+1000,first+60000));}
el('show-run').onclick=showRun;el('show-history').onclick=()=>timeline.setWindow(-1000,Math.min(maximum+1000,60000));
if(reviewHistory.length)el('show-history').onclick();else showRun();
el('run-bounds').textContent=`${ordered.length} chronological steps. Optimization test: steps ${(stepPositions.get('0')||0)/1000+1}–${(stepPositions.get(String(events.length-1))||0)/1000+1}. Drag to pan; scroll/pinch or use Zoom buttons. Drag the pointer or click a symbol to inspect. Matching colors identify prediction/label classes.`;
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
el('end').value=events.length;
function stop(){if(timer!==null)clearInterval(timer);timer=null;el('play').textContent='Play';}
function revealPointer(point){
 const window=timeline.getWindow();
 if(point.valueOf()<+window.start||point.valueOf()>+window.end)timeline.moveTo(point,{animation:false});
}
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
 if(event&&stepPositions.has(String(position))){
  const point=new Date(stepPositions.get(String(position)));
  currentStep=point.valueOf()/1000;
  if(!cursorAdded){timeline.addCustomTime(point,'playback');cursorAdded=true;}
  else timeline.setCustomTime(point,'playback');
  revealPointer(point);
 }
 el('configuration').textContent=readable(snapshots[position]||'Configuration not captured at this point');
 el('raw-event').textContent=event?pretty(event):'No recorded events';
 inspectEvent(event);
 el('back').disabled=!ordered.length||currentStep===0;el('next').disabled=!ordered.length||currentStep===ordered.length-1;el('play').disabled=!events.length;
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
 stop();
 const source=reviewHistory[index],row=source.record;
 const point=new Date(stepPositions.get('source:'+index));if(cursorAdded)timeline.setCustomTime(point,'playback');else{timeline.addCustomTime(point,'playback');cursorAdded=true;}
 currentStep=point.valueOf()/1000;
 revealPointer(point);
 el('status').textContent=`Step ${currentStep+1}/${ordered.length} · original reviewer record`;
 el('back').disabled=currentStep===0;el('next').disabled=currentStep===ordered.length-1;
 el('event-fields').replaceChildren();el('paired-request').hidden=true;el('content-box').open=false;
 el('event-title').textContent=source.source_table==='presentations'?`Predicted ${row.predicted_label} · ${Math.round(row.confidence*100)}%`:`Human ${row.action}: ${row.label||'—'}`;
 el('event-summary').textContent=`Original reviewer record · ${row.shown_at||row.created_at} · ${source.article.assignment}. Not generated by this optimization run.`;
 for(const [label,value] of Object.entries({Title:source.article.title,Abstract:source.article.abstract,Explanation:row.comment,'Prediction shown before vote':source.presentation?`${source.presentation.predicted_label} (${Math.round(source.presentation.confidence*100)}%)`:undefined}))if(value){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;el('event-fields').append(dt,dd);}
 el('event-content').textContent=readable(source);el('raw-event').textContent=pretty(row);el('configuration').textContent='Historical source record: configuration not reconstructed from later optimization state.';
}
function move(index){stop();position=Math.max(0,Math.min(events.length-1,index));draw();}
el('back').onclick=()=>goStep(currentStep-1);el('next').onclick=()=>goStep(currentStep+1);
el('round').onchange=()=>{if(el('round').value!=='')move(Number(el('round').value));};
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
