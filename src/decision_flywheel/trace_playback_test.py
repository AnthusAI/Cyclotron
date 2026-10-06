"""Execute viewer controls against a stub DOM, without browser or network access."""
import json
import re
import shutil
import subprocess

import pytest

from .trace_artifact import render_trace


def test_operational_prediction_details_link_the_actual_request_and_disagreement_filters_keep_both_labels():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    events = [
        {'kind': 'cycle-started', 'item': {'id': 'paper', 'values': {'text': 'Title: Paper'}}},
        {'kind': 'decision-request', 'target_id': 'paper', 'state': {'examples': []}, 'questions': {}},
        {'kind': 'decision-response', 'target_id': 'paper', 'answers': {'decision': {'choice': 'reject'}}},
        {'kind': 'prediction', 'target_id': 'paper', 'label': 'reject', 'probabilities': {'reject': .8, 'accept': .2}},
        {'kind': 'human-feedback', 'feedback': {'item_id': 'paper', 'final_answer_value': 'accept'}},
        {'kind': 'cycle-completed'},
    ]
    events = [{**e, 'event_id': i+1, 'cycle_id': 'one', 'cycle_number': 1,
               'created_at': f'2026-10-06T12:00:0{i}Z'} for i, e in enumerate(events)]
    html = render_trace(events)
    data = dict(re.findall(r'<script id="([^"]+)" type="application/json">(.*?)</script>', html, re.S))
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    harness = r'''
const assert=require('node:assert/strict'),elements={},handlers={};
for(const [id,text] of Object.entries(DATA))elements[id]={textContent:text};
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){},replaceChildren(){},addEventListener(n,f){this[n]=f;},getBoundingClientRect(){return {left:0,width:1000};}}),createElement:()=>({textContent:'',style:{},append(){},setAttribute(){}})};
global.vis={Timeline:class{constructor(c,i,g,o){this.options=o;this.window=[100,300];}on(n,f){handlers[n]=f;}redraw(){}addCustomTime(){}setCustomTime(){}setItems(i){this.items=i;}setWindow(a,b){this.window=[+a,+b];}getWindow(){return {start:this.window[0],end:this.window[1]}}moveTo(){}fit(){}}};
CODE
handlers.select({items:[3]});
assert.equal(elements['paired-request'].hidden,false);
elements['paired-request'].onclick();assert.equal(position,1);
assert.ok(elements['event-content'].textContent.includes('examples'));
assert.equal(timeline.options.zoomMin,4);
assert.equal(elements['show-history'].hidden,true);
handlers.doubleClick({item:3});
assert.ok(timeline.window[1]-timeline.window[0]<1000);
elements['show-run'].onclick();assert.deepEqual(timeline.window,[0,1000]);
timeline.setWindow(100,300);
const gesture=(dx,dy,extra={})=>({deltaX:dx,deltaY:dy,deltaMode:0,clientX:500,preventDefault(){},stopImmediatePropagation(){},...extra});
elements.timeline.wheel(gesture(100,10));
assert.deepEqual(timeline.window,[120,320]);
elements.timeline.wheel(gesture(-100,0));assert.deepEqual(timeline.window,[100,300]);
elements.timeline.wheel(gesture(100,10,{timeStamp:1000}));
elements.timeline.wheel(gesture(10,100,{timeStamp:1010}));
assert.equal(timeline.window[1]-timeline.window[0],200);
elements.timeline.wheel(gesture(0,100));assert.ok(timeline.window[1]-timeline.window[0]>200);
timeline.setWindow(100,101);elements['zoom-out'].onclick();assert.ok(timeline.window[1]-timeline.window[0]>=4);
elements['zoom-out'].onclick();assert.ok(timeline.window[1]-timeline.window[0]>=8);
timeline.setWindow(0,4);elements['fit-all'].onclick();assert.deepEqual(timeline.window,[0,1000]);
elements['disagreement-filter'].checked=true;applyFilters();
assert.ok(timeline.items.some(i=>i.className==='marker-prediction'));
assert.ok(timeline.items.some(i=>i.className==='marker-human'));
'''.replace('DATA', json.dumps(data)).replace('CODE', code)
    result = subprocess.run(['node'], input=harness, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_clicking_event_shows_only_that_event_and_filters_do_not_change_recorded_history():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    events = []
    for name in ('a', 'b'):
        for kind, extra in [('step-started', {'step_stage': 'rubric'}),
                            ('optimizer-request', {'messages': [{'content': 'prompt-'+name}]}),
                            ('optimizer-response', {'content': 'reply-'+name, 'tool_calls': [{'name': 'tool-'+name}]}),
                            ('step-completed', {'result': {'reason': 'outcome-'+name}})]:
            events.append({'event_id': len(events)+1, 'kind': kind, 'step_id': name,
                           'created_at': '2026-10-06T12:00:00Z', **extra})
    for label in ('accept', 'reject'):
        events.append({'event_id': len(events)+1, 'kind': 'human-feedback',
            'created_at': '2026-10-06T12:01:00Z', 'assignment': 'development',
            'feedback': {'final_answer_value': label, 'edit_comment_value': 'reason' if label=='accept' else ''}})
    events.append({'event_id':11,'kind':'internal-cache-check','created_at':'2026-10-06T12:01:10Z'})
    history=[{'source_table':'review_events','record':{'label':'accept','action':'vote','comment':'why','created_at':'2026-10-06T12:02:00Z'},
              'article':{'title':'Source title','abstract':'Source abstract','assignment':'train'},'presentation':{'predicted_label':'reject','confidence':.9}}]
    history.append({'source_table':'presentations','record':{'predicted_label':'reject','confidence':.9,'shown_at':'2026-10-06T12:01:30Z'},
                    'article':{'title':'Source title','abstract':'Source abstract','assignment':'train'}})
    html = render_trace(events, history)
    data = dict(re.findall(r'<script id="([^"]+)" type="application/json">(.*?)</script>', html, re.S))
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    harness = r'''
const assert=require('node:assert/strict');
const elements={};
for(const [id,text] of Object.entries(DATA))elements[id]={textContent:text};
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){},replaceChildren(){},addEventListener(n,f){this[n]=f;},getBoundingClientRect(){return {left:0,width:1000};}}),createElement:()=>({textContent:'',style:{},append(){},setAttribute(){}})};
let select;const handlers={};
global.vis={Timeline:class{constructor(c,i,g,o){this.options=o;this.groups=g;}on(name,fn){handlers[name]=fn;if(name==='select')select=fn;}redraw(){this.redrawn=true;}addCustomTime(){}setCustomTime(){}setItems(items){this.items=items;}setWindow(a,b){this.window=[+a,+b];}getWindow(){return {start:0,end:1000}}moveTo(point){this.center=+point}fit(){}}};
CODE
select({items:[1]});
assert.equal(position,1);
assert.ok(elements['event-content'].textContent.includes('prompt-a'));
assert.ok(!elements['event-content'].textContent.includes('reply-a'));
assert.equal(elements['content-box'].open,true);
select({items:[2]});
assert.ok(elements['event-content'].textContent.includes('reply-a'));
assert.ok(elements['event-content'].textContent.includes('tool-a'));
assert.ok(!elements['event-content'].textContent.includes('reply-b'));
assert.equal(elements['paired-request'].hidden,false);
elements['paired-request'].onclick();assert.equal(position,1);
select({items:[2]});
assert.equal(elements['raw-event'].textContent,JSON.stringify(events[2],null,2));
const old=JSON.stringify(events);applyFilters();assert.equal(JSON.stringify(events),old);
const inspectorBody={scrollTop:500};elements['inspector'].querySelector=()=>inspectorBody;
select({items:[2]});assert.equal(inspectorBody.scrollTop,0);
elements['label-filter'].value='accept';applyFilters();
assert.equal(timeline.items.filter(i=>i.group?.startsWith('label-class-')).length,2);
assert.ok(timeline.items.filter(i=>i.group?.startsWith('label-class-'))[0].title.includes('accept'));
elements['label-filter'].value='';elements['comment-filter'].checked=true;applyFilters();
assert.equal(timeline.items.filter(i=>i.group?.startsWith('label-class-')).length,2);
assert.ok(timeline.groups.some(g=>g.id==='model-decisions'&&Array.isArray(g.nestedGroups)));
assert.ok(timeline.groups.some(g=>g.id==='human-labels'&&g.nestedGroups.length===2));
assert.equal(elements['content-box'].open,false);
select({items:['source:0']});
assert.ok(elements['event-title'].textContent.includes('accept'));
assert.ok(elements['event-content'].textContent.includes('Source abstract'));
assert.ok(elements['event-content'].textContent.includes('reject'));
assert.equal(timeline.options.stack,false);
assert.equal(timeline.options.zoomable,true);
assert.equal(timeline.options.preferZoom,true);
assert.equal(timeline.options.selectable,true);
assert.equal(timeline.options.verticalScroll,false);
assert.equal(timeline.options.maxHeight,undefined);
assert.equal(timeline.options.horizontalScroll,false);
assert.equal(timeline.options.horizontalScrollKey,'shiftKey');
assert.equal(timeline.options.horizontalScrollInvert,true);
assert.equal(timeline.options.zoomKey,'');
assert.equal(timeline.options.zoomMin,100);
assert.equal(typeof timeline.options.onInitialDrawComplete,'function');
assert.ok(HTML.includes('data-ui="shadcn"'));
assert.ok(HTML.includes('pointer-events:none'));
assert.ok(!HTML.includes('}.vis-group{min-height:36px}'));
assert.ok(!HTML.includes('id="view-seek"'));
elements['zoom-in'].onclick();assert.equal(timeline.window[1]-timeline.window[0],500);
elements['zoom-out'].onclick();assert.equal(timeline.window[1]-timeline.window[0],2000);
handlers.timechanged({id:'playback',time:new Date(stepPositions.get('1'))});assert.equal(position,1);
elements['next'].onclick();assert.equal(position,2);assert.equal(currentStep,ordered.findIndex(record=>record.key==='2'));
assert.ok(timeline.options.format.minorLabels(new Date(1000)).includes('2'));
assert.ok(timelineItems.every(i=>i.content.textContent.length===1));
assert.ok(timelineItems.every(i=>i.type==='box'));
assert.equal(timelineItems.find(i=>i.source_index===0).className,'marker-human');
assert.equal(timelineItems.find(i=>i.source_index===1).className,'marker-prediction');
assert.ok(timelineItems.some(i=>i.className==='marker-optimizer-request'));
assert.ok(timelineItems.some(i=>i.className==='marker-optimizer-response'));
assert.equal(ordered.length,timelineItems.length);
assert.equal(stepPositions.has('10'),false);
assert.ok(!HTML.includes('id="label-legend"'));
elements['close-inspector'].onclick();
assert.equal(elements.inspector.hidden,true);
assert.equal(elements.workspace.className,'workspace inspector-closed');
assert.equal(elements['show-inspector'].hidden,false);
assert.equal(timeline.redrawn,true);
select({items:[1]});
assert.equal(elements.inspector.hidden,false);
assert.equal(elements.workspace.className,'workspace');
assert.equal(elements['show-inspector'].hidden,true);
elements['disagreement-filter'].checked=true;applyFilters();
assert.equal(timeline.items.filter(i=>i.type==='box').length,1);
assert.equal(timeline.items.find(i=>i.type==='box').source_index,0);
assert.ok(timeline.items.some(i=>i.type==='background'&&String(i.id).startsWith('cycle-band:')));
assert.ok(timeline.items.some(i=>i.group==='step-items'&&i.type==='range'));
select({items:['step-label:'+projection.steps.findIndex(s=>s.keys.includes('source:0'))]});
assert.ok(elements['event-title'].textContent.includes('accept'));
assert.equal(new Set(timelineItems.map(i=>+i.start)).size,timelineItems.length);
assert.equal(readable({messages:[{role:'user',content:JSON.stringify({human_explanations:['knowledge bases'],feedback:[{text:'Title\\nAbstract'}]})}]}).includes('Title\\n'),true);
assert.ok(!readable({content:'line one\\nline two'}).includes('\\\\n'));
assert.ok(readable({content:'<script>untrusted</script>'}).includes('<script>untrusted</script>'));
'''.replace('DATA', json.dumps(data)).replace('HTML', json.dumps(html)).replace('CODE', code)
    completed = subprocess.run(['node'], input=harness, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
