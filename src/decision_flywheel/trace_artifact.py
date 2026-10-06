"""Offline, self-contained playback of private recorded flywheel events."""
import argparse
import json
from pathlib import Path
import sqlite3
from .trace_timeline import timeline_data
from .trace_rounds import round_details
from .trace_step_projection import step_projection
from .trace_classification import class_configuration, positive_metrics, running_metric_series


def read_trace(database):
    uri = Path(database).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as db:
        return [{**json.loads(payload), 'event_id': number}
                for number, payload in db.execute('SELECT id,payload FROM runtime_events ORDER BY id')]


def render_trace(events, reviewer_history=(), *, class_config=None, run_comparison=None):
    if class_config is None and run_comparison is not None:
        class_config = run_comparison.get('class_config')
    def encode(value):
        data = json.dumps(value, ensure_ascii=True, allow_nan=False)
        return data.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    vendor = Path(__file__).parent / 'vendor' / 'vis-timeline'
    viewer = Path(__file__).parent / 'vendor' / 'trace-ui'
    javascript = (vendor / 'standalone/umd/vis-timeline-graph2d.min.js').read_text()
    css = (vendor / 'styles/vis-timeline-graph2d.min.css').read_text()
    projected = step_projection(events, reviewer_history,
                                [item for item in timeline_data(events)['items'] if item['group']!='configuration-count'])
    labels = []
    for event in events:
        labels.extend(event.get('classifier_snapshot',{}).get('config',{}).get('task',{}).get('labels',[]))
        if event.get('kind')=='prediction': labels.append(event['label'])
        if event.get('kind')=='human-feedback' and event.get('feedback',{}).get('final_answer_value') is not None:
            labels.append(event['feedback']['final_answer_value'])
    for source in reviewer_history:
        label=source['record'].get('label') or source['record'].get('predicted_label')
        if label is not None: labels.append(label)
    classes=class_configuration(labels,class_config)
    metric_view={}
    for index,event in enumerate(events):
        result=event.get('result') or event
        if isinstance(result,dict) and ('incumbent' in result or 'candidate' in result):
            metric_view[str(index)]={key:positive_metrics(result.get(key) or {},classes) for key in ('incumbent','candidate')}
    return TEMPLATE.replace('__RECORDING__', encode(events)).replace(
        '__CLASS_CONFIG__',encode(classes)).replace('__RUN_COMPARISON__',encode(run_comparison)).replace('__METRIC_VIEW__',encode(metric_view)).replace('__METRIC_SERIES__',encode(running_metric_series(events,classes))).replace(
        '__PRESENTATION__', encode(recover_configurations(events))).replace(
        '__REVIEW_HISTORY__', encode(reviewer_history)).replace('__EXCHANGES__', encode(exchange_indices(events))).replace('__ROUNDS__', encode(round_details(events))).replace(
        '__TIMELINE_DATA__', encode(timeline_data(events))).replace('__STEP_PROJECTION__', encode(projected)).replace(
        '__TIMELINE_JS__', javascript.replace('</script', '<\\/script')).replace('__TIMELINE_CSS__', css).replace(
        '__VENDOR_LICENSE__', encode((vendor / 'LICENSE.MIT.txt').read_text() + '\n\n' +
        (viewer / 'THIRD_PARTY_NOTICES.txt').read_text())).replace(
        '__VIEWER_CSS__', (viewer / 'viewer.css').read_text()).replace(
        '__VIEWER_JS__', (viewer / 'viewer.js').read_text().replace('</script', '<\\/script'))


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
    parser.add_argument('--class-config', type=Path, help='JSON ordered class labels and positive/negative/neutral roles')
    parser.add_argument('--run-comparison', type=Path, help='JSON matched endpoint evaluation; never inferred from running scores')
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error('choose a new output path; recorded artifacts are not overwritten')
    if args.output.suffix != '.html':
        parser.error('output must be an HTML file')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        from .trace_review_history import read_review_history
        stream.write(render_trace(read_trace(args.database), read_review_history(args.reviews) if args.reviews else (),
            class_config=json.loads(args.class_config.read_text()) if args.class_config else None,
            run_comparison=json.loads(args.run_comparison.read_text()) if args.run_comparison else None))
    print(f'Private offline trace: {args.output.resolve()}')


TEMPLATE = r'''<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; font-src data:; connect-src 'none'">
<title>Decision Flywheel — run explorer</title>
<script>
window.addEventListener('error',event=>{const node=document.getElementById('runtime-health');if(node){node.textContent='Viewer error: '+event.message;node.style.color='red';}});
window.addEventListener('securitypolicyviolation',event=>{const node=document.getElementById('runtime-health');if(node){node.textContent='Browser blocked '+event.violatedDirective;node.style.color='red';}});
</script>
<style>__TIMELINE_CSS__</style>
<style>__VIEWER_CSS__</style>
<script>__TIMELINE_JS__</script>
<div id="root" data-ui="shadcn"></div>
<noscript>This offline explorer requires JavaScript to display the timeline.</noscript>
<script id="recording" type="application/json">__RECORDING__</script>
<script id="presentation" type="application/json">__PRESENTATION__</script>
<script id="exchanges" type="application/json">__EXCHANGES__</script>
<script id="round-data" type="application/json">__ROUNDS__</script>
<script id="timeline-data" type="application/json">__TIMELINE_DATA__</script>
<script id="step-projection" type="application/json">__STEP_PROJECTION__</script>
<script id="review-history" type="application/json">__REVIEW_HISTORY__</script>
<script id="vendor-license-data" type="application/json">__VENDOR_LICENSE__</script>
<script id="class-config" type="application/json">__CLASS_CONFIG__</script>
<script id="run-comparison" type="application/json">__RUN_COMPARISON__</script>
<script id="metric-view" type="application/json">__METRIC_VIEW__</script>
<script id="metric-series" type="application/json">__METRIC_SERIES__</script>
<script>__VIEWER_JS__</script>
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
const projection=JSON.parse(el('step-projection').textContent);
const ordered=projection.order.map(key=>({key}));
const stepPositions=new Map(Object.entries(projection.positions));
const classConfig=JSON.parse(el('class-config').textContent),metricView=JSON.parse(el('metric-view').textContent);
const classes=classConfig.map(row=>row.label);
function agreement(item){
 if(item.source_index!==undefined){
  const source=reviewHistory[item.source_index],row=source.record;
  if(source.source_table==='review_events')return row.label&&source.presentation?row.label===source.presentation.predicted_label?'match':'mismatch':'unreviewed';
  const vote=reviewHistory.findLast(s=>s.source_table==='review_events'&&row.id!==undefined&&s.record.presentation_id===row.id&&s.record.action==='vote'&&s.record.label);
  return vote?vote.record.label===row.predicted_label?'match':'mismatch':'unreviewed';
 }
 const event=events[item.event_index];if(!event?.cycle_id)return 'unreviewed';
 const target=event.target_id||event.feedback?.item_id;
 const vote=events.findLast(e=>e.kind==='human-feedback'&&e.cycle_id===event.cycle_id&&e.feedback?.item_id===target);
 const prediction=events.find(e=>e.kind==='prediction'&&e.cycle_id===event.cycle_id&&e.target_id===target);
 return vote&&prediction?(vote.feedback.final_answer_value===prediction.label?'match':'mismatch'):'unreviewed';
}
function markerIcon(item,name){
 item.iconName=name;item.content.textContent='';
 const svg=el('icon-'+name)?.firstElementChild?.cloneNode(true);
 if(svg)item.content.append(svg);else item.content.textContent=name==='circle-plus'?'+':name==='circle-minus'?'−':'○';
}
for(const item of timelineItems){
 item.title=item.content.textContent;item.start=new Date(stepPositions.get(String(item.event_index??item.id)));
 const source=item.source_index!==undefined?reviewHistory[item.source_index]:null,event=events[item.event_index];
 const label=source?(source.record.label||source.record.predicted_label):(event?.label||event?.feedback?.final_answer_value);
 item.className=source?(source.source_table==='presentations'?'marker-prediction':'marker-human'):
  event?.kind==='human-feedback'?'marker-human':event?.kind==='prediction'?'marker-prediction':`marker-${event?.kind||'event'}`;
 if(label&&item.className==='marker-prediction')item.group='prediction-class-'+classes.indexOf(label);
 else if(label&&item.className==='marker-human')item.group='label-class-'+classes.indexOf(label);
 else if(item.group==='decisions')item.group='decision-api';
 item.type='box';
 item.content=document.createElement('span');item.content.textContent=event?.kind==='trigger-evaluated'?(event.due?'⚑':'○'):item.className==='marker-human'?'◆':item.className==='marker-prediction'?'●':item.className==='marker-optimizer-request'?'□':item.className==='marker-optimizer-response'?'■':'•';
 item.content.setAttribute('aria-label',item.title);
 if(item.group==='configuration-count'){
  item.content.textContent=item.title;item.className='marker-classification-count';
  item.title+= ' active classifications (main decision plus supporting questions)';
 }
 if(label&&(item.className==='marker-prediction'||item.className==='marker-human')){
  item.agreement=agreement(item);item.className+=' agreement-'+item.agreement;
  item.content.textContent='';
  item.title+=` · ${item.agreement==='match'?'Matches human label':item.agreement==='mismatch'?'Prediction and human label disagree':'No paired prediction/human label'}`;
 }else if(event?.kind==='trigger-evaluated'){
  item.className+=event.due?' trigger-fired':' trigger-idle';markerIcon(item,event.due?'circle-play':'circle');
 }
 const metrics=evaluationMetrics(event);
 if(metrics&&metrics.rows[0].candidate!==null){
  item.content.textContent='';const glyph=document.createElement('span');glyph.className='outcome-glyph';
  for(const value of [metrics.rows[0].incumbent,metrics.rows[0].candidate]){const bar=document.createElement('span');bar.style.height=`${Math.max(2,20*(value??0))}px`;glyph.append(bar);}
  item.content.append(glyph);item.title+=` · accuracy ${metrics.rows[0].incumbent===null?'unavailable':Math.round(metrics.rows[0].incumbent*100)+'%'} → ${Math.round(metrics.rows[0].candidate*100)}%`;
 }
}
const classGroups=[];
for(const [parent,prefix,title] of [['model-decisions','prediction-class-','Model decisions'],['human-labels','label-class-','Human labels']]){
 const children=[];
 classes.forEach((label,index)=>{const id=prefix+index;if(!timelineItems.some(item=>item.group===id))return;
  const heading=document.createElement('span');heading.textContent=label;
  children.push({id,content:heading});});
 if(children.length)classGroups.push({id:parent,content:title,nestedGroups:children.map(g=>g.id),showNested:true},...children);
}
const occupied=new Set(timelineItems.map(i=>i.group));
const otherGroups=timelineData.groups.filter(g=>occupied.has(g.id)&&!['decisions'].includes(g.id));
// API exchanges belong in the classification inspector, not a separate lane.
const plottedItems=timelineItems.filter(item=>!['decision-api','cycles'].includes(item.group)&&events[item.event_index]?.kind!=='cycle-metrics');
for(const item of plottedItems){if(item.group==='classifier')item.group='classifier-attempts';if(item.group==='evaluation')item.group='optimization-outcomes';}
for(const group of otherGroups)if(group.id==='feedback')group.content='Review actions (skip / undo)';
const optimizationLanes=[['triggers','Triggers'],['rubric','Rubric'],['examples','Few-shot examples'],['questions','Classifier questions'],['classifier','ML optimization']].map(([id,content])=>({id,content}));
const optimizationIds=optimizationLanes.map(group=>group.id);
optimizationLanes.find(group=>group.id==='classifier').nestedGroups=['classifier-attempts','fit'];
optimizationLanes.find(group=>group.id==='classifier').showNested=true;
optimizationIds.push('optimization-outcomes');
timelineData.groups=[...classGroups,
 {id:'optimization',content:'Optimization',nestedGroups:optimizationIds,showNested:true},...optimizationLanes,
 {id:'classifier-attempts',content:'Proposals / trials'},{id:'fit',content:'Fitting'},
 {id:'optimization-outcomes',content:'Optimization outcomes'},
 ...otherGroups.filter(group=>!optimizationIds.includes(group.id)&&!['cycles','evaluation','fit','configuration','configuration-count'].includes(group.id)),
 {id:'configuration-group',content:'Configuration',nestedGroups:['configuration','configuration-count'],showNested:true},
 {id:'configuration',content:'Changes'},{id:'configuration-count',content:'Classifications'},
 {id:'evaluation-trends',content:'Evaluation',nestedGroups:['metric-accuracy','metric-precision','metric-recall'],showNested:true},
 ...['accuracy','precision','recall'].map(metric=>({id:'metric-'+metric,content:metric[0].toUpperCase()+metric.slice(1)+' · 0–100%'}))].map((group,order)=>({...group,order}));
const metricSeries=JSON.parse(el('metric-series').textContent),trendItems=[];
for(const metric of ['accuracy','precision','recall']){
 let previous=null;
 for(const point of metricSeries){
  const value=point[metric],x=stepPositions.get(String(point.event_index));
  if(value===null||value===undefined||x===undefined){previous=null;continue;}
  const y=58-50*Math.max(0,Math.min(1,value));
  const content=document.createElement('span');
  content.innerHTML=`<svg viewBox="0 0 20 66" width="20" height="66" aria-hidden="true"><circle cx="10" cy="${y}" r="3" fill="currentColor"/></svg>`;
  const title=`${metric}: ${Math.round(value*1000)/10}% · Cycle ${point.cycle_number??'unrecorded'} · ${point.count} reviewed items · ${point.scope||'recorded running performance'}${metric==='accuracy'?'':' · positive: '+point.positive_labels.join(', ')}`;
  trendItems.push({id:`metric:${metric}:${point.event_index}`,event_index:point.event_index,group:'metric-'+metric,start:new Date(x),type:'box',className:'metric-point metric-'+metric,content,title});
  if(previous){
   const line=document.createElement('span');line.innerHTML=`<svg viewBox="0 0 100 66" width="100%" height="66" preserveAspectRatio="none" aria-hidden="true"><path d="M0 ${previous.y} L100 ${y}" fill="none" stroke="currentColor" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>`;
   trendItems.push({id:`metric-line:${metric}:${point.event_index}`,event_index:point.event_index,group:'metric-'+metric,start:new Date(previous.x),end:new Date(x),type:'range',className:'metric-line metric-'+metric,content:line,title});
  }
  previous={x,y};
 }
}
// Internal step positions remain available for seeking and playback, but do
// not need their own display lane. Cycle bands retain the item context.
const stepItems=trendItems;
const cycleItems=projection.cycles.flatMap((cycle,index)=>{
 return [{id:'cycle-band:'+index,start:new Date(cycle.start),end:new Date(cycle.end),type:'background',className:cycle.recorded?(index%2?'cycle-band-even':'cycle-band-odd'):'cycle-band-history',content:''}];
});
const maximum=Math.max(1000,...projection.cycles.map(c=>c.end));
const cellDetails=new Map();
const cycleByEvent=new Map(projection.cycles.flatMap(c=>c.keys.map(key=>[key,c])));
function optimizationActivity(cycleId,stage,recording=events){
 const rows=recording.filter(event=>event.cycle_id===cycleId);
 const work=rows.filter(event=>(event.step_stage||event.stage)===stage);
 const ran=work.some(event=>event.kind==='optimizer-request');
 const completed=work.some(event=>event.kind==='optimizer-response');
 const failed=work.some(event=>['optimizer-failed','step-failed'].includes(event.kind));
 const accepted=work.some(event=>(event.result||event).activated===true||(event.result||event).promoted===true||event.kind==='classifier-activated');
 return {ran,completed,failed,accepted};
}
function rubricActivity(cycleId,recording=events){
 const rows=recording.filter(event=>event.cycle_id===cycleId);
 const {ran,completed,failed,accepted}=optimizationActivity(cycleId,'rubric',recording);
 const before=rows.find(event=>event.kind==='cycle-started')?.classifier_snapshot?.config?.rubric;
 const after=rows.findLast(event=>['cycle-completed','cycle-failed','classifier-activated'].includes(event.kind))?.classifier_snapshot?.config?.rubric;
 const changed=typeof before==='string'&&typeof after==='string'?before!==after:null;
 const changeLabel=changed===null?'Change unknown':changed?'Changed':'Unchanged';
 return {ran,completed,failed,before,after,changed,accepted,
  label:failed?'Failed':ran&&!completed?'Running':`${ran?'Ran':'No LLM call'} · ${changeLabel}`};
}
function cycleCells(items){
 if(projection.axis!=='cycle')return items;
 const buckets=new Map(),passthrough=[];
 for(const item of items){
  const cycle=cycleByEvent.get(String(item.event_index));
  if(!cycle){passthrough.push(item);continue;}
  const key=`cell:${cycle.key}:${item.group}`;
  if(!buckets.has(key))buckets.set(key,{cycle,items:[]});
  buckets.get(key).items.push(item);
 }
 return [...passthrough,...[...buckets].flatMap(([id,bucket])=>{
  const members=bucket.items,cycle=bucket.cycle;
  const rubric=members[0].group==='rubric';
  const questions=members[0].group==='questions';
  const first=rubric||questions?(members.findLast(m=>events[m.event_index]?.kind==='optimizer-response')||members.findLast(m=>events[m.event_index]?.kind==='optimizer-request')||members[0]):members[0].group==='configuration-count'?members.at(-1):members.find(m=>m.className.includes('trigger-fired'))||members[0];
  const activity=rubric?rubricActivity(cycle.key):null;
  const questionActivity=questions?optimizationActivity(cycle.key,'questions'):null;
  if(rubric&&!activity.ran)return [];
  const trigger=first.group==='triggers';
  if(trigger&&!members.some(member=>member.className.includes('trigger-fired')))return [];
  const decision=first.className.includes('marker-prediction')||first.className.includes('marker-human');
  const content=document.createElement('span');
  if(!rubric&&!questions&&!decision&&!trigger){
   const representative=members.find(m=>m.className.includes('trigger-fired'))||members.at(-1);
   if(members.length===1||first.group==='configuration-count'||first.group==='triggers')content.append(representative.content.cloneNode(true));
   else content.textContent=String(members.length);
  }
  cellDetails.set(id,[first,...members.filter(member=>member!==first)]);
  return [{...first,id,start:new Date(cycle.start),end:new Date(cycle.end),type:'range',content,
   className:first.className+(decision?' cycle-decision-cell':trigger?' cycle-trigger-cell':' cycle-event-cell')+(rubric?' rubric-status-cell '+(activity.accepted?'rubric-accepted':'rubric-not-accepted'):'')+(questions?' '+(questionActivity.accepted?'optimization-accepted':'optimization-not-accepted'):''),
   title:rubric?`Rubric optimizer: ${activity.label} · ${activity.accepted?'Accepted':'Not accepted'}`:questions?`Classifier question optimization: ${questionActivity.accepted?'Accepted':'Not accepted'}`:members.map(m=>m.title).join('\n')}];
 })];
}
plottedItems.splice(0,plottedItems.length,...cycleCells(plottedItems));
const minimumWindow=projection.axis==='cycle'?4:100;
let initialWindowSet=false;
const timeline=new vis.Timeline(el('timeline'),[...cycleItems,...stepItems,...plottedItems],timelineData.groups,{onInitialDrawComplete:()=>{if(!initialWindowSet){initialWindowSet=true;timeline.setWindow(0,maximum,{animation:false});}},height:'100%',editable:false,selectable:true,showCurrentTime:false,stack:false,stackSubgroups:false,orientation:'top',min:0,max:maximum,zoomMin:minimumWindow,zoomMax:maximum,verticalScroll:true,horizontalScroll:false,horizontalScrollKey:'shiftKey',horizontalScrollInvert:true,zoomKey:'ctrlKey',moveable:true,zoomable:true,preferZoom:false,showMajorLabels:false,format:{minorLabels:date=>projection.axis==='cycle'&&date.valueOf()%1000!==0?'':`${projection.axis==='cycle'?'Cycle':'Step'} ${Math.floor(date.valueOf()/1000)+1}`}});
function setView(start,width){
 width=Math.min(maximum,Math.max(minimumWindow,Math.ceil(width)));
 start=Math.round(Math.max(0,Math.min(maximum-width,start)));
 timeline.setWindow(start,start+width,{animation:false});
}
function zoomView(factor,anchor=.5){
 const window=timeline.getWindow(),start=+window.start,width=+window.end-start;
 const next=Math.min(maximum,Math.max(minimumWindow,factor>1?Math.max(width+1,Math.ceil(width*factor)):width*factor));
 setView(start+width*anchor-next*anchor,next);
}
el('zoom-in').onclick=()=>zoomView(.5);
el('zoom-out').onclick=()=>zoomView(2);
el('fit-all').onclick=()=>{const window=timeline.getWindow(),width=Math.min(maximum,4000);setView((+window.start+ +window.end-width)/2,width);};
// Capture before vis-timeline: its default wheel path treats diagonal/horizontal
// trackpad gestures as zoom. Ordinary scrolling navigates; only pinch zooms.
let wheelAxis=null,lastWheelAt=-Infinity,fullscreen=false;
function updateFullscreen(active){
 fullscreen=active;wheelAxis=null;lastWheelAt=-Infinity;
 el('workspace').classList?.toggle('is-fullscreen',active);
 const button=el('fullscreen-toggle'),label=active?'Exit fullscreen':'Enter fullscreen';
 button.setAttribute?.('aria-label',label);button.title=label;
 el('navigation-hint').textContent='Horizontal scroll: pan · vertical scroll: rows · pinch: zoom';
 timeline.redraw();
}
el('fullscreen-toggle').onclick=async()=>{
 if(fullscreen){if(document.fullscreenElement&&document.exitFullscreen)await document.exitFullscreen();updateFullscreen(false);}
 else {updateFullscreen(true);try{await el('workspace').requestFullscreen?.();}catch{/* Fixed-viewport fallback when native fullscreen is unavailable. */}}
};
document.addEventListener?.('fullscreenchange',()=>updateFullscreen(Boolean(document.fullscreenElement)));
document.addEventListener?.('keydown',event=>{if(event.key==='Escape'&&fullscreen&&!document.fullscreenElement)updateFullscreen(false);});
el('timeline').addEventListener('wheel',event=>{
 const rect=(el('timeline').querySelector?.('.vis-panel.vis-center')||el('timeline')).getBoundingClientRect();
 const scale=event.deltaMode===1?16:event.deltaMode===2?rect.width:1;
 const dx=event.deltaX*scale,dy=event.deltaY*scale;
 if(!dx&&!dy)return;
 // Browsers expose trackpad pinch as a control-modified wheel gesture.
 if(event.ctrlKey){
  event.preventDefault();event.stopImmediatePropagation();
  const anchor=Math.max(0,Math.min(1,(event.clientX-rect.left)/Math.max(1,rect.width)));
  zoomView(Math.exp(Math.max(-120,Math.min(120,dy))*.004),anchor);
  return;
 }
 const stamped=Number.isFinite(event.timeStamp);
 if(!stamped||!wheelAxis||event.timeStamp-lastWheelAt>180||event.shiftKey)
  wheelAxis=event.shiftKey||Math.abs(dx)>Math.abs(dy)?'pan':'rows';
 lastWheelAt=stamped?event.timeStamp:-Infinity;
 // Vertical wheel gestures scroll rows, including in fullscreen.
 if(wheelAxis==='rows')return;
 event.preventDefault();event.stopImmediatePropagation();
 if(wheelAxis==='pan'){
  const window=timeline.getWindow(),width=+window.end- +window.start;
  setView(+window.start+(event.shiftKey&&Math.abs(dy)>Math.abs(dx)?dy:dx)*width/Math.max(1,rect.width),width);
 }
},{capture:true,passive:false});
function setInspectorOpen(open){
 el('inspector').hidden=!open;
 el('show-inspector').hidden=open;
 el('workspace').className=(open?'workspace':'workspace inspector-closed')+(fullscreen?' is-fullscreen':'');
 timeline.redraw();
}
el('close-inspector').onclick=()=>setInspectorOpen(false);
el('show-inspector').onclick=()=>setInspectorOpen(true);
timeline.on('timechanged',properties=>{if(properties.id==='playback')goStep(ordered.reduce((best,record,index)=>Math.abs(stepPositions.get(record.key)-properties.time.valueOf())<Math.abs(stepPositions.get(ordered[best].key)-properties.time.valueOf())?index:best,0));});
function goStep(step,pause=true){
 if(pause)stop();const bounded=Math.max(0,Math.min(ordered.length-1,step)),key=ordered[bounded]?.key;
 if(key===undefined)return;
 if(key.startsWith('source:'))inspectSource(Number(key.split(':')[1]),pause);else move(Number(key),pause);
}
function showRun(){timeline.setWindow(0,maximum,{animation:false});}
el('show-history').hidden=!reviewHistory.length;
el('show-run').textContent='Recorded cycles';
el('show-run').onclick=showRun;el('show-history').onclick=()=>{const cycle=projection.cycles.find(c=>c.key==='source-history');timeline.setWindow(cycle?.start||0,cycle?.end||maximum,{animation:false});};
const sourceSteps=ordered.filter(record=>record.key.startsWith('source:')).map(record=>Math.floor(stepPositions.get(record.key)/1000)+1);
el('run-bounds').textContent=`${projection.cycles.filter(c=>c.recorded).length} operational cycles · ${projection.steps.length} internal steps · ${ordered.length} event markers. Each cycle processes an item, optionally receives feedback and evaluates triggers. Optimization/backfill requests stay inside the cycle that caused them. Circle: trigger not due; flag: triggered work. Older records without operational cycle IDs are not presented as cycles. Vertical scroll moves through rows in both modes. Pinch or zoom buttons change scale. Horizontal scroll or drag pans. Click headings or markers for details.`;
for(const [id,values] of [['label-filter',[...events.filter(e=>e.kind==='human-feedback').map(e=>String(e.feedback?.final_answer_value??'unlabeled')),...reviewHistory.filter(s=>s.source_table==='review_events'&&s.record.label).map(s=>s.record.label)]],
 ['role-filter',[...events.filter(e=>e.kind==='human-feedback').map(e=>e.assignment||'unassigned'),...reviewHistory.map(s=>s.article.assignment)]]]){
 for(const value of id==='label-filter'?classes:[...new Set(values)].sort()){const option=document.createElement('option');option.value=value;option.textContent=value;el(id).append(option);}
}
function applyFilters(){
 timeline.setItems([...cycleItems,...stepItems,...plottedItems.filter(item=>{
 if(el('disagreement-filter').checked){
  if(item.source_index===undefined){
   const event=events[item.event_index];
   const vote=events.find(e=>e.kind==='human-feedback'&&e.cycle_id===event.cycle_id);
   const prediction=events.find(e=>e.kind==='prediction'&&e.cycle_id===event.cycle_id);
   if(!vote||!prediction||vote.feedback?.final_answer_value===prediction.label)return false;
  }else{
  const source=reviewHistory[item.source_index],vote=source.source_table==='review_events'?source:reviewHistory.find(s=>s.source_table==='review_events'&&source.record.id!==undefined&&s.record.presentation_id===source.record.id&&s.record.action==='vote');
  if(!vote||!vote.record.label||!vote.presentation||vote.record.label===vote.presentation.predicted_label)return false;
  }
 }
 if(item.source_index!==undefined){const source=reviewHistory[item.source_index];if(source.source_table!=='review_events')return true;
  return (!el('label-filter').value||source.record.label===el('label-filter').value)&&(!el('role-filter').value||source.article.assignment===el('role-filter').value)&&(!el('comment-filter').checked||Boolean(source.record.comment));}
 const e=events[item.event_index];if(e.kind!=='human-feedback')return true;
  return (!el('label-filter').value||String(e.feedback?.final_answer_value??'unlabeled')===el('label-filter').value)
   &&(!el('role-filter').value||(e.assignment||'unassigned')===el('role-filter').value)
   &&(!el('comment-filter').checked||Boolean(e.feedback?.edit_comment_value));})]);
}
for(const id of ['label-filter','role-filter','comment-filter','disagreement-filter'])el(id).onchange=applyFilters;
function showCellEvents(members){
 const host=el('cell-events');host.replaceChildren();host.hidden=members.length<2;
 for(const member of [...members].sort((a,b)=>a.event_index-b.event_index)){
  const button=document.createElement('button');const event=events[member.event_index];
  button.textContent=`${event?.kind||member.title} · ${event?.event_id??''}`;
  button.onclick=()=>{const key=String(member.id);move(member.event_index);if(key.startsWith('configuration-count:'))showConfiguration(member.event_index);showCellEvents(members);};
  host.append(button);
 }
}
timeline.on('select',properties=>{if(properties.items.length){setInspectorOpen(true);let id=properties.items[0];const members=cellDetails.get(id);if(members)id=members[0].id;const countMarker=String(id).startsWith('configuration-count:');if(countMarker||String(id).startsWith('metric:')||String(id).startsWith('metric-line:'))id=String(id).split(':').at(-1);else if(String(id).startsWith('cycle-'))id=projection.cycles[Number(String(id).split(':')[1])].keys[0];else if(String(id).startsWith('step-'))id=projection.steps[Number(String(id).split(':')[1])].keys[0];if(String(id).startsWith('source:'))inspectSource(Number(String(id).split(':')[1]));else move(Number(id));if(countMarker||['classifier-activated','classifier-invalidated'].includes(events[Number(id)]?.kind)){showConfiguration(Number(id));}showCellEvents(members||[]);}});
function showConfiguration(index){
 const snapshot=snapshots[index],config=snapshot?.config||snapshot?.configuration;
 el('event-title').textContent='Active configuration';
 el('event-content').textContent=config?readable({classification_count:1+(config.tasks||[]).length,
  main_decision:config.task,supporting_questions:config.tasks||[],rubric:config.rubric,
  example_ids:config.example_ids||[],dynamic_elements:config.dynamic_elements||[],
  learned_head_present:!!snapshot.head,learned_features:snapshot.head?.feature_names||[],
  validation_status:snapshot.validation_status}):'No recorded configuration at this point';
 el('content-title').textContent='Recorded configuration';el('content-box').open=true;el('cycle-states').hidden=true;
}
timeline.on('doubleClick',properties=>{
 const id=properties.item;if(id===undefined||id===null)return;
 const key=String(id);
 const range=cellDetails.has(key)?cycleByEvent.get(String(cellDetails.get(key)[0].event_index)):key.startsWith('cycle-')?projection.cycles[Number(key.split(':')[1])]:key.startsWith('step-')?projection.steps[Number(key.split(':')[1])]:projection.steps.find(s=>s.keys.includes(key));
 if(range)timeline.setWindow(range.start,range.end,{animation:false});
});
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
position=0;el('start').value=1;
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
 el('status').textContent=event?`${event.cycle_number?'Cycle '+event.cycle_number+' · ':''}Event ${event.event_id} · ${event.kind} · ${event.step_stage||(event.cycle_id?'item processing':'legacy/unscoped')} · ${event.status||event.reason||''}`:'No recorded events';
 if(event&&stepPositions.has(String(position))){
  const point=new Date(stepPositions.get(String(position)));
  currentStep=ordered.findIndex(record=>record.key===String(position));
  if(!cursorAdded){timeline.addCustomTime(point,'playback');cursorAdded=true;}
  else timeline.setCustomTime(point,'playback');
  revealPointer(point);
 }
 el('configuration').textContent=readable(snapshots[position]||'Configuration not captured at this point');
 el('raw-event').textContent=event?pretty(event):'No recorded events';
 inspectEvent(event);
 el('back').disabled=!ordered.length||currentStep===0;el('next').disabled=!ordered.length||currentStep===ordered.length-1;el('play').disabled=!events.length;
}
// Pair recorded exchanges by target and execution scope, not by item alone:
// the same article can be rescored repeatedly with different candidate contexts.
function decisionExchange(index){
 const selected=events[index],scope=event=>JSON.stringify([event.cycle_id,event.step_id,event.target_id]);
 if(!selected?.target_id)return {request:null,response:null};
 let pending=null;
 for(let i=0;i<events.length;i++){
  const event=events[i];if(scope(event)!==scope(selected))continue;
  if(event.kind==='decision-request')pending=i;
  else if(event.kind==='decision-response'){
   if(i===index||pending===index)return {request:pending===null?null:events[pending],response:event};
   pending=null;
  }
 }
 return {request:selected.kind==='decision-request'?selected:null,response:null};
}
function showDecisionExchange(exchange){
 el('decision-exchange').hidden=false;
 el('decision-exchange-summary').textContent=`Request event ${exchange.request?.event_id??'not recorded'} · response event ${exchange.response?.event_id??'not recorded'} · ${exchange.request?.model||exchange.response?.model||'model not recorded'}`;
 el('decision-request-tab').focus?.();
 el('decision-request-content').textContent=exchange.request?readable({model:exchange.request.model,state:exchange.request.state,questions:exchange.request.questions}):'No matching request was recorded. It has not been reconstructed from a later configuration.';
 el('decision-response-content').textContent=exchange.response?readable(exchange.response):'No matching response was recorded.';
}
function showOptimizerExchange(index){
 const owner=roundData.owners[String(index)],round=roundData.rounds[String(owner)];
 el('optimizer-exchange').hidden=true;
 el('optimizer-proposal').hidden=true;
 if(!round||(!round.optimizer_requests.length&&!round.optimizer_responses.length))return;
 el('optimizer-exchange').hidden=false;
 el('optimizer-exchange-summary').textContent=`Request event ${round.optimizer_requests.map(i=>events[i].event_id).join(', ')||'not recorded'} · response event ${round.optimizer_responses.map(i=>events[i].event_id).join(', ')||'not recorded'} · ${events[round.optimizer_requests[0]]?.requested_model||'model not recorded'}`;
 el('optimizer-request-tab').focus?.();
 el('optimizer-request-content').textContent=round.optimizer_requests.length?readable(round.optimizer_requests.map(i=>{
  const e=events[i];return {event_id:e.event_id,model:e.requested_model,messages:e.messages};
 })):'No optimizer request was recorded for this attempt.';
 el('optimizer-response-content').textContent=round.optimizer_responses.length?readable(round.optimizer_responses.map(i=>{
  const e=events[i];return {event_id:e.event_id,model:e.model,content:e.content,tool_calls:e.tool_calls||[],usage:e.usage};
 })):'No optimizer response was recorded for this attempt.';
 const proposal=events[round.proposals.at(-1)];
 if(proposal){
  el('optimizer-proposal').hidden=false;
  const changes={};
  for(const key of ['rubric','example_ids','tasks','dynamic_elements']){
   if(JSON.stringify(proposal.previous?.[key])!==JSON.stringify(proposal.candidate?.[key]))changes[key]={before:proposal.previous?.[key]??'Not recorded',proposed:proposal.candidate?.[key]??'Not recorded'};
  }
  el('optimizer-proposal-content').textContent=readable(changes);
  const terminal=round.outcomes.map(i=>events[i]).findLast(e=>typeof (e.result||e).promoted==='boolean'),result=terminal?.result||terminal;
  el('optimizer-proposal-status').textContent=`Validation is a proposal check, not activation. ${result?`${result.activated?'Activated provisionally — improvement not established':result.promoted?'Promoted into the active classifier':'Not promoted; active configuration unchanged'} · ${result.reason||'No reason recorded'}${result.candidate?.count!==undefined?' · '+result.candidate.count+' development items':''}`:'No promotion outcome was recorded.'}`;
 }
}
function evaluationMetrics(event){
 if(!event)return null;
 const result=event.result||event,incumbent=result.incumbent||{},candidate=result.candidate||{};
 if(!result.incumbent&&!result.candidate)return null;
 const score=value=>typeof value==='number'&&Number.isFinite(value)?value:null;
 const rows=[{label:'Accuracy',incumbent:score(incumbent.accuracy),candidate:score(candidate.accuracy)},
  {label:'Balanced accuracy',incumbent:score(incumbent.balanced_accuracy),candidate:score(candidate.balanced_accuracy)},
  {label:'Precision',incumbent:score(incumbent.macro_precision??incumbent.precision),candidate:score(candidate.macro_precision??candidate.precision)}];
 const view=metricView[String(events.indexOf(event))];
 if(view?.candidate.positive_labels.length){
  const positive=view.candidate.positive_labels.join(', ');
  rows[2]={label:`Precision · positive (${positive})`,incumbent:view.incumbent.precision,candidate:view.candidate.precision,
   incumbentUnavailable:view.incumbent.predicted_positive_count===0?'Undefined':'Unavailable',candidateUnavailable:view.candidate.predicted_positive_count===0?'Undefined':'Unavailable'};
  rows.push({label:`Recall · positive (${positive})`,incumbent:view.incumbent.recall,candidate:view.candidate.recall,
   incumbentUnavailable:view.incumbent.actual_positive_count===0?'Undefined':'Unavailable',candidateUnavailable:view.candidate.actual_positive_count===0?'Undefined':'Unavailable'});
 }
 for(const label of classes.filter(label=>label in (incumbent.per_class||{})||label in (candidate.per_class||{}))){
  for(const metric of ['recall','precision'])rows.push({label:`${metric==='recall'?'Recall':'Precision'} · ${label}`,incumbent:score(incumbent.per_class?.[label]?.[metric]),candidate:score(candidate.per_class?.[label]?.[metric])});
 }
 return {rows,count:candidate.count??incumbent.count,reason:result.reason,promoted:result.promoted};
}
function showEvaluation(event){
 const metrics=evaluationMetrics(event);el('evaluation-visual').hidden=!metrics;
 el('evaluation-bars').replaceChildren();if(!metrics)return;
 el('evaluation-summary').textContent=`${metrics.count??'Unknown number of'} evaluation items · ${metrics.promoted===true?'Promoted':metrics.promoted===false?'Not promoted':'Promotion not recorded'}${metrics.reason?' · '+metrics.reason:''}. Gray: incumbent. Solid: candidate. Missing metrics are not estimated.`;
 for(const row of metrics.rows){
  const group=document.createElement('div');group.className='metric-row';
  const title=document.createElement('div');title.className='metric-label';title.textContent=row.label;group.append(title);
  for(const key of ['incumbent','candidate']){
   const series=document.createElement('div');series.className='metric-series';
   const name=document.createElement('span');name.textContent=key==='incumbent'?'Incumbent':'Candidate';
   const track=document.createElement('div');track.className='metric-track';
   const fill=document.createElement('div');fill.className='metric-fill '+key;fill.style.width=`${Math.max(0,Math.min(1,row[key]??0))*100}%`;track.append(fill);
   const value=document.createElement('span');value.textContent=row[key]===null?(row[key+'Unavailable']||'Not recorded'):`${Math.round(row[key]*1000)/10}%`;
   series.append(name,track,value);group.append(series);
  }
  el('evaluation-bars').append(group);
 }
}
function inspectEvent(event){
 el('cell-events').hidden=true;
 el('inspector').scrollTop=0;
 const body=el('inspector').querySelector?.('.inspector-body');if(body)body.scrollTop=0;
 el('event-fields').replaceChildren();
 el('content-box').open=false;
 el('paired-request').hidden=true;
 el('decision-exchange').hidden=true;
 el('content-box').hidden=false;
 showOptimizerExchange(position);
 showEvaluation(event);
 el('cycle-states').hidden=true;
 if(!event){el('event-title').textContent='No recorded events';return;}
 el('event-title').textContent=event.kind.replaceAll('-',' ');
 el('event-summary').textContent=`Event ${event.event_id} · ${event.created_at||'time unavailable'} · ${event.step_stage||(event.cycle_id?'item processing':'unscoped')}`;
 const field=(label,value)=>{if(value===undefined||value===null)return;const term=document.createElement('dt'),description=document.createElement('dd');term.textContent=label;description.textContent=typeof value==='object'?readable(value):String(value);el('event-fields').append(term,description);};
 const itemStep=projection.steps.find(step=>step.keys.includes(String(position)));
 if(itemStep?.item_id)field('Item',itemStep.title);
 const cycle=projection.cycles.find(c=>c.keys.includes(String(position)));
 if(cycle)field('Cycle',cycle.title);
 if(event.kind==='trigger-evaluated'){field('Stage',event.stage);field('Run',event.due);field('Reason',event.reason);field('Trigger inputs',event.details);}
 if(event.trigger_event_id)field('Caused by trigger event',event.trigger_event_id);
 if(event.kind==='cycle-metrics'){
  field('Measured scope',event.metric_scope);field('Reviewed items',event.metrics?.count);
  const point=metricSeries.find(point=>point.event_index===position);
  if(point){field('Running accuracy',point.accuracy===null?'Unavailable':`${Math.round(point.accuracy*1000)/10}%`);
   field('Positive classes',point.positive_labels.join(', ')||'Not configured');
   field('Running precision',point.precision===null?'Undefined / unavailable':`${Math.round(point.precision*1000)/10}%`);
   field('Running recall',point.recall===null?'Undefined / unavailable':`${Math.round(point.recall*1000)/10}%`);}
 }
 let payload=event,title='Full event content';
 if(event.cycle_id&&(event.step_stage||event.stage)==='rubric'){
  const activity=rubricActivity(event.cycle_id);
  field('Rubric optimization',activity.label);
  field('Rubric before this cycle',activity.before===undefined?'Not recorded':activity.before||'(empty)');
  field('Rubric after this cycle',activity.after===undefined?'Not recorded':activity.after||'(empty)');
 }
 if(event.experiment==='single-example-swap'){
  field('Example removed',event.examples?.removed||event.removed_id);field('Example added',event.examples?.added||event.added_id);
  field('Brier improvement',event.brier_gain);field('Accuracy change',event.accuracy_change);
  field('Question probability changes by human class',event.question_effects);
 }
 if(event.kind==='example-ranking-completed'){
  field('Measurement scope',event.scope);field('Effect scope',event.effect_scope);
  field('Development class counts',event.by_class);field('Ranked example swaps',event.rankings);
 }
 if(event.kind==='human-feedback'){
  field('Label',event.feedback?.final_answer_value);field('Partition',event.assignment);field('Action',event.action);
  field('Explanation',event.feedback?.edit_comment_value);field('Item',event.feedback?.item_id);
 }else if(event.kind==='prediction'){
  field('Predicted class',event.label);field('Confidence',event.confidence);
  field('Class probabilities',event.probabilities);field('Classifier version',event.version);
  const start=events.find(e=>e.kind==='cycle-started'&&e.cycle_id===event.cycle_id);
  field('Article',start?.item?.values);
  const requests=events.filter(e=>e.kind==='decision-request'&&e.cycle_id===event.cycle_id&&e.target_id===event.target_id);
  const responses=events.filter(e=>e.kind==='decision-response'&&e.cycle_id===event.cycle_id&&e.target_id===event.target_id);
  const cached=events.filter(e=>e.kind==='features-cached'&&e.cycle_id===event.cycle_id&&e.target_id===event.target_id);
  field('Decision request events',requests.map(e=>e.event_id));
  field('Decision response events',responses.map(e=>e.event_id));
  payload={prediction:event,requests,responses,cached};title='Exact decision exchange';
  const actualRequest=events.findLastIndex((e,index)=>index<position&&e.kind==='decision-request'&&e.cycle_id===event.cycle_id&&e.step_id===event.step_id&&e.target_id===event.target_id);
  if(actualRequest>=0){showDecisionExchange(decisionExchange(actualRequest));el('content-box').hidden=true;}
  if(requests.length){el('paired-request').hidden=false;el('paired-request').textContent='Open actual decision request';el('paired-request').onclick=()=>move(events.indexOf(requests[0]));}
 }else if(['cycle-started','cycle-completed','cycle-failed'].includes(event.kind)){
  const before=events.find(e=>e.kind==='cycle-started'&&e.cycle_id===event.cycle_id);
  const after=events.findLast(e=>['cycle-completed','cycle-failed'].includes(e.kind)&&e.cycle_id===event.cycle_id);
  el('event-title').textContent=`Cycle ${event.cycle_number} · recorded states`;
  el('cycle-states').hidden=false;
  el('cycle-before').textContent=readable(before?.classifier_snapshot||'Before state was not recorded.');
  el('cycle-after').textContent=readable(after?.classifier_snapshot||'After state was not recorded.');
  el('cycle-state-summary').textContent=`${after?.kind==='cycle-failed'?'Failed':after?'Completed':'No recorded completion'} · Before event ${before?.event_id??'unavailable'} → after event ${after?.event_id??'unavailable'}. These are recorded snapshots, not reconstructed states.`;
  field('Article',before?.item?.values);field('Reason',event.reason);
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
  if(request!==undefined){el('paired-request').hidden=false;el('paired-request').textContent='Open matching optimizer request';el('paired-request').onclick=()=>move(request);}
 }else if(event.kind==='candidate-evaluated'){
  for(const key of ['accuracy','balanced_accuracy','balanced_brier']){field('Incumbent '+key,event.incumbent?.[key]);field('Candidate '+key,event.candidate?.[key]);}
  field('Reason',event.reason);field('Independent evaluation',event.evaluation_independent_of_optimizer_context);
 }else if(event.kind==='decision-request'||event.kind==='decision-response'){
  const exchange=decisionExchange(position),request=exchange.request;
  showDecisionExchange(exchange);el('content-box').hidden=true;
  field('Model',request?.model||event.model);field('Target',event.target_id);
  field('Examples',request?.state?.examples?.length);field('Questions',Object.keys(request?.questions||{}).join(', '));
  field('Request event',request?.event_id);field('Response event',exchange.response?.event_id);
  if(request&&event.kind==='decision-response'){
   el('paired-request').hidden=false;el('paired-request').textContent='Go to matching decision request';
   el('paired-request').onclick=()=>move(events.indexOf(request));
  }
 }else{
  field('Reason',event.reason||event.result?.reason);field('Status',event.status);field('Training items',event.training_count);field('Feature count',event.features?.length);
  field('Promoted',event.promoted??event.result?.promoted);
 }
 el('content-title').textContent=title;el('event-content').textContent=readable(payload);
}
function inspectSource(index,pause=true){
 el('cell-events').hidden=true;
 if(pause)stop();
 el('inspector').scrollTop=0;
 const body=el('inspector').querySelector?.('.inspector-body');if(body)body.scrollTop=0;
 const source=reviewHistory[index],row=source.record;
 const point=new Date(stepPositions.get('source:'+index));if(cursorAdded)timeline.setCustomTime(point,'playback');else{timeline.addCustomTime(point,'playback');cursorAdded=true;}
 currentStep=ordered.findIndex(record=>record.key==='source:'+index);
 revealPointer(point);
 el('status').textContent=`Step ${Math.floor(point.valueOf()/1000)+1}/${projection.steps.length} · original reviewer record`;
 el('back').disabled=currentStep===0;el('next').disabled=currentStep===ordered.length-1;
 el('event-fields').replaceChildren();el('paired-request').hidden=true;el('content-box').open=false;el('content-box').hidden=false;el('decision-exchange').hidden=true;el('optimizer-exchange').hidden=true;el('evaluation-visual').hidden=true;el('cycle-states').hidden=true;
 el('event-title').textContent=source.source_table==='presentations'?`Predicted ${row.predicted_label} · ${Math.round(row.confidence*100)}%`:`Human ${row.action}: ${row.label||'—'}`;
 el('event-summary').textContent=`Original reviewer record · ${row.shown_at||row.created_at} · ${source.article.assignment}. Not generated by this optimization run.`;
 for(const [label,value] of Object.entries({Title:source.article.title,Abstract:source.article.abstract,Explanation:row.comment,'Prediction shown before vote':source.presentation?`${source.presentation.predicted_label} (${Math.round(source.presentation.confidence*100)}%)`:undefined}))if(value){const dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;el('event-fields').append(dt,dd);}
 el('event-content').textContent=readable(source);el('raw-event').textContent=pretty(row);el('configuration').textContent='Historical source record: configuration not reconstructed from later optimization state.';
}
function move(index,pause=true){if(pause)stop();position=Math.max(0,Math.min(events.length-1,index));draw();}
el('back').onclick=()=>goStep(currentStep-1);el('next').onclick=()=>goStep(currentStep+1);
el('round').onchange=()=>{if(el('round').value!=='')move(Number(el('round').value));};
el('play').onclick=()=>{
 if(timer!==null){stop();return;}
 const first=Number(el('start').value)-1,last=Number(el('end').value)-1;
 if(!Number.isInteger(first)||!Number.isInteger(last)||first<0||last>=events.length||first>last){el('status').textContent='Choose a valid event range';return;}
 // Playback follows the same visible event order as Previous/Next. Never jump
 // to a later optimizer round or spend ticks on unplotted internal records.
 el('play').textContent='Pause';
 timer=setInterval(()=>{
  const next=currentStep+1,record=ordered[next];
  if(!record||(!record.key.startsWith('source:')&&Number(record.key)>last)){stop();return;}
  goStep(next,false);
  if(currentStep===ordered.length-1)stop();
 },1000);
};
if(reviewHistory.length)inspectSource(Number(ordered.find(record=>record.key.startsWith('source:')).key.split(':')[1]));else draw();
el('runtime-health').textContent=`Build: visible-steps-v2. Interactive viewer started: ${timelineItems.length} markers. Native clicks / range changes: 0.`;
let interactionCount=0;
function recordInteraction(){interactionCount++;el('runtime-health').textContent=`Build: visible-steps-v2. Interactive viewer started: ${timelineItems.length} markers. Native clicks / range changes: ${interactionCount}.`;}
timeline.on('rangechanged',recordInteraction);
const markerPositions=[...new Set(plottedItems.map(item=>+item.start))].sort((a,b)=>a-b);
let markerSize=20;
function updateMarkerScale(){
 const window=timeline.getWindow(),start=+window.start,end=+window.end;
 const plot=el('timeline').querySelector('.vis-panel.vis-center');
 const width=plot?.clientWidth||el('timeline').clientWidth;
 const visible=markerPositions.filter(position=>position>=start&&position<=end);
 const gaps=visible.slice(1).map((position,index)=>position-visible[index]).filter(gap=>gap>0).sort((a,b)=>a-b);
 // Typical local event spacing, rather than cycle width: multiple steps can
 // occupy one cycle. Ignore rare tiny gaps so one cluster cannot shrink all icons.
 const spacing=gaps.length?gaps[Math.floor(gaps.length/2)]:1000;
 const slotWidth=width*spacing/Math.max(1,end-start);
 const size=Math.round(Math.min(20,Math.max(5,slotWidth*.7))*10)/10;
 if(size===markerSize)return;
 markerSize=size;
 el('timeline').style.setProperty('--timeline-marker-size',`${size}px`);
 el('timeline').style.setProperty('--timeline-marker-padding',`${Math.min(4,size*.2)}px`);
 timeline.redraw();
}
timeline.on('rangechange',updateMarkerScale);
timeline.on('rangechanged',updateMarkerScale);
if(typeof ResizeObserver!=='undefined')new ResizeObserver(updateMarkerScale).observe(el('timeline'));
else window.addEventListener('resize',updateMarkerScale);
updateMarkerScale();
timeline.on('click',properties=>{
 recordInteraction();
 if(String(properties.item).startsWith('cycle-band:')){setInspectorOpen(true);const cycle=projection.cycles[Number(String(properties.item).split(':')[1])];goStep(ordered.findIndex(record=>record.key===cycle.keys[0]));return;}
 // Marker selection is handled by select; empty plot/axis clicks seek to the
 // exact clicked moment and anchor transport controls to the preceding event.
 if(properties.item!==null&&properties.item!==undefined)return;
 if(!properties.time||!ordered.length)return;
 const point=new Date(Math.max(0,Math.min(maximum,+properties.time)));
 const cycle=projection.axis==='cycle'?projection.cycles.find(c=>+point>=c.start&&+point<c.end):null;
 const preceding=cycle?ordered.findIndex(record=>record.key===cycle.keys[0]):ordered.reduce((best,record,index)=>stepPositions.get(record.key)<=+point?index:best,0);
 goStep(preceding);
 timeline.setCustomTime(point,'playback');
});
</script></html>'''


if __name__ == '__main__':
    main()
