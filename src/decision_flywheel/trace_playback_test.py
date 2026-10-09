"""Execute viewer controls against a stub DOM, without browser or network access."""
import json
import re
import shutil
import subprocess

import pytest

from .trace_artifact import render_trace


def test_fit_all_restores_the_entire_recorded_range_after_deep_zoom():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    code=re.findall(r'<script>(.*?)</script>',render_trace([]),re.S)[-1]
    controls=code[code.index('function setView('):code.index('// Capture before vis-timeline:')]
    harness="""
const assert=require('node:assert/strict');
const maximum=12000,minimumWindow=4;
const elements={};const el=id=>elements[id]??(elements[id]={});
const timeline={window:[6500,6504],getWindow(){return {start:this.window[0],end:this.window[1]}},setWindow(start,end){this.window=[start,end]}};
CONTROLS
el('fit-all').onclick();assert.deepEqual(timeline.window,[0,12000]);
el('zoom-in').onclick();assert.deepEqual(timeline.window,[3000,9000]);
el('fit-all').onclick();assert.deepEqual(timeline.window,[0,12000]);
""".replace('CONTROLS',controls)
    result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_playback_dispatches_only_recorded_model_comparisons_at_or_before_the_cursor():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    code=re.findall(r'<script>(.*?)</script>',render_trace([]),re.S)[-1]
    dispatch=code[code.index(' const calibrationSnapshots={};'):code.index(" el('status').textContent=")]
    harness="""
const assert=require('node:assert/strict');
const classConfig=[{label:'include',role:'positive'}];
const events=[{kind:'prediction'},
 {kind:'cycle-metrics',classifier_id:'Topic',class_config:[{label:'yes',role:'positive'}],metrics:{decision_model_comparison:{count:1}}},
 {kind:'cycle-metrics',classifier_id:'Topic',metrics:{decision_model_comparison:{count:2}}}];
let position=1, delivered={};
class CustomEvent{constructor(name,options){this.name=name;this.detail=options.detail;}}
const window={dispatchEvent:event=>delivered[event.name]=event.detail};
function dispatch(){DISPATCH}
dispatch();
assert.equal(delivered['flywheel-model-comparison-position'].Topic.comparison.count,1);
assert.deepEqual(delivered['flywheel-model-comparison-position'].Topic.classes,[{label:'yes',role:'positive'}]);
position=2;dispatch();assert.equal(delivered['flywheel-model-comparison-position'].Topic.comparison.count,2);
position=0;dispatch();assert.deepEqual(delivered['flywheel-model-comparison-position'],{});
""".replace('DISPATCH',dispatch)
    result=subprocess.run(['node','-e',harness],capture_output=True,text=True)
    assert result.returncode==0,result.stderr


def test_selection_details_expose_priorities_and_raw_selection_is_not_called_a_head_activation():
    html = render_trace([])
    assert "field('Candidate objective scores'" in html
    assert "field('Incumbent objective scores'" in html
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    helpers = code[code.index('function optimizationActivity('):code.index('function cycleCells(')]
    result = subprocess.run(['node', '-e', helpers + """
const assert=require('node:assert/strict');
const rows=[{cycle_id:'c',stage:'classifier',kind:'classifier-training-completed',promoted:true,selected:{feature_set:'raw_decision'}}];
assert.equal(mlActivity('c','classifier-attempts',rows).label,'Raw decision model selected; no learned head');
"""], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_a_long_run_opens_with_every_cycle_visible():
    if not shutil.which('node'):
        pytest.skip('Node is needed for viewer interaction spec')
    html = render_trace([])
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    callback = re.search(r'onInitialDrawComplete:(.*?),height:', code).group(1)
    result = subprocess.run(['node', '-e',
        "const assert=require('node:assert/strict');let initialWindowSet=false;"
        "const maximum=87000,projection={axis:'cycle'};"
        "const timeline={setWindow:(start,end)=>assert.deepEqual([start,end],[0,maximum])};"
        f"({callback})();"], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


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
    events[0]['classifier_snapshot'] = {'config': {'rubric': 'before rubric'}, 'head': None}
    events[-1]['classifier_snapshot'] = {'config': {'rubric': 'after rubric'}, 'head': {'weights': [1]}}
    html = render_trace(events, class_config=[{'label':'accept','role':'positive'},{'label':'reject','role':'negative'}])
    data = dict(re.findall(r'<script id="([^"]+)" type="application/json">(.*?)</script>', html, re.S))
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    harness = r'''
const assert=require('node:assert/strict'),elements={},handlers={};
for(const [id,text] of Object.entries(DATA))elements[id]={textContent:text};
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){},replaceChildren(){},addEventListener(n,f){this[n]=f;},getBoundingClientRect(){return {left:0,width:1000};}}),createElement:()=>({textContent:'',style:{},cloneNode(){return {...this};},append(...children){this.textContent+=children.map(child=>child.textContent||'').join('');},setAttribute(){}})};
global.vis={Timeline:class{constructor(c,i,g,o){this.options=o;this.window=[100,300];}on(n,f){handlers[n]=f;}redraw(){}addCustomTime(){}setCustomTime(){}setCustomTimeTitle(title,id){this.cursorTitle={title,id};}setItems(i){this.items=i;}setWindow(a,b){this.window=[+a,+b];}getWindow(){return {start:this.window[0],end:this.window[1]}}moveTo(){}fit(){}}};
global.CustomEvent=class{constructor(type,options){this.type=type;this.detail=options.detail;}};
global.window={addEventListener(){},dispatchEvent(){}};
Object.assign(document.getElementById('timeline'),{clientWidth:1000,querySelector(){return null;},style:{setProperty(){}}});
let tick;global.setInterval=fn=>{tick=fn;return 1;};global.clearInterval=()=>{tick=null;};
CODE
assert.deepEqual(classes,['accept','reject']);
assert.equal(timelineItems.find(item=>item.event_index===3).iconName,undefined);
assert.equal(timelineItems.find(item=>item.event_index===4).iconName,undefined);
const cells=cycleCells(plottedItems);
const decisionCell=cells.find(item=>item.event_index===3);
assert.equal(decisionCell.type,'range');
assert.equal(+decisionCell.start,0);assert.equal(+decisionCell.end,1000);
assert.ok(decisionCell.className.includes('cycle-decision-cell'));
const idleTrigger={...timelineItems[0],group:'triggers',className:'marker-trigger-evaluated trigger-idle'};
const firedTrigger={...timelineItems[1],group:'triggers',className:'marker-trigger-evaluated trigger-fired'};
assert.equal(cycleCells([idleTrigger]).length,0);
const triggerCell=cycleCells([idleTrigger,firedTrigger])[0];
assert.equal(triggerCell.content.textContent,'');
assert.ok(triggerCell.className.includes('cycle-trigger-cell'));
assert.ok(triggerCell.className.includes('trigger-fired'));
assert.equal(+triggerCell.end-+triggerCell.start,1000);
const rubricFixture=[
 {kind:'cycle-started',cycle_id:'rubric-test',classifier_snapshot:{config:{rubric:'old'}}},
 {kind:'optimizer-request',cycle_id:'rubric-test',step_stage:'rubric'},
 {kind:'optimizer-response',cycle_id:'rubric-test',step_stage:'rubric'},
 {kind:'proposal-validated',cycle_id:'rubric-test',step_stage:'rubric',proposal:{rubric:'new'}},
 {kind:'cycle-completed',cycle_id:'rubric-test',classifier_snapshot:{config:{rubric:'old'}}}
];
assert.equal(rubricActivity('rubric-test',rubricFixture).label,'Ran · Unchanged');
assert.equal(configurationCountChange('rubric-test','questions',rubricFixture),null);
rubricFixture[0].classifier_snapshot.config.tasks=[];
rubricFixture[4].classifier_snapshot.config.tasks=[{name:'support'}];
rubricFixture[0].classifier_snapshot.config.example_ids=[];
rubricFixture[4].classifier_snapshot.config.example_ids=['a','b'];
assert.equal(configurationCountChange('rubric-test','questions',rubricFixture),2);
assert.equal(configurationCountChange('rubric-test','examples',rubricFixture),2);
rubricFixture[0].classifier_snapshot.config.tasks=[{name:'other'}];
assert.equal(configurationCountChange('rubric-test','questions',rubricFixture),null);
assert.equal(rubricActivity('rubric-test',rubricFixture).accepted,false);
rubricFixture.push({kind:'step-completed',cycle_id:'rubric-test',step_stage:'rubric',result:{activated:true,promoted:false}});
assert.equal(rubricActivity('rubric-test',rubricFixture).accepted,true);
rubricFixture.pop();
rubricFixture[4].classifier_snapshot.config.rubric='new';
assert.equal(rubricActivity('rubric-test',rubricFixture).label,'Ran · Changed');
assert.equal(rubricActivity('rubric-test',rubricFixture.slice(0,2)).label,'Running');
assert.equal(rubricActivity('rubric-test',[rubricFixture[1],rubricFixture[2]]).label,'Ran · Change unknown');
const noCall=timelineItems.filter(item=>[1,2].includes(item.event_index)).map(item=>({...item,group:'rubric'}));
assert.equal(cycleCells(noCall).length,0);
events[1].step_stage='rubric';events[1].kind='optimizer-request';
events[2].step_stage='rubric';events[2].kind='optimizer-response';
const rubricCell=cycleCells(noCall)[0];
assert.equal(rubricCell.content.textContent,'');
assert.ok(rubricCell.className.includes('rubric-status-cell'));
events[1].kind='decision-request';events[2].kind='decision-response';
const exchangeCells=cycleCells(timelineItems.filter(item=>[1,2].includes(item.event_index)).map(item=>({...item,group:'examples'})));
assert.equal(exchangeCells.length,1);
const questionCells=cycleCells(timelineItems.filter(item=>[1,2].includes(item.event_index)).map(item=>({...item,group:'questions'})));
assert.equal(questionCells[0].content.textContent,'');
assert.ok(questionCells[0].className.includes('optimization-not-accepted'));
const mlItems=timelineItems.filter(item=>[1,2].includes(item.event_index)).map(item=>({...item,group:'fit'}));
assert.ok(cycleCells(mlItems)[0].className.includes('optimization-not-accepted'));
events[2].kind='classifier-activated';events[2].classifier_snapshot={head:{weights:{include:1}}};
const acceptedFit=cycleCells(mlItems)[0];
assert.ok(acceptedFit.className.includes('optimization-accepted'));
assert.equal(acceptedFit.content.textContent,'');
events[2].kind='decision-response';delete events[2].classifier_snapshot;
assert.deepEqual(cellDetails.get(exchangeCells[0].id).map(item=>item.event_index),[1,2]);
handlers.select({items:[exchangeCells[0].id]});
assert.equal(position,1);assert.equal(elements['cell-events'].hidden,false);
assert.ok(elements['decision-request-content'].textContent.includes('examples'));
assert.equal(timelineItems.find(item=>item.event_index===3).agreement,'mismatch');
assert.equal(timelineItems.find(item=>item.event_index===4).agreement,'mismatch');
events[4].feedback.final_answer_value='reject';assert.equal(agreement({event_index:3}),'match');
events[4].feedback.final_answer_value='accept';
events[4].feedback.item_id='another paper';assert.equal(agreement({event_index:3}),'unreviewed');events[4].feedback.item_id='paper';
const savedVote=events[4];events[4]={kind:'nothing'};assert.equal(agreement({event_index:3}),'unreviewed');events[4]=savedVote;
assert.ok(!timelineData.groups.some(group=>group.id==='cycles'));
assert.ok(!timelineData.groups.some(group=>group.id==='flywheel-cycles'));
assert.deepEqual(timelineData.groups.find(group=>group.id==='configuration-group').nestedGroups,['configuration','configuration-count']);
const initialCount=timelineItems.find(item=>item.group==='configuration-count');
const sameCount={...initialCount,id:'same-count',event_index:1,content:document.createElement('span')};
const countCell=cycleCells([initialCount,sameCount])[0];
assert.equal(countCell.content.textContent,'1');
assert.equal(cellDetails.get(countCell.id).length,2);
handlers.select({items:['configuration-count:0']});
assert.deepEqual(timeline.cursorTitle,{title:'',id:'playback'});
assert.equal(position,0);
assert.equal(elements['event-title'].textContent,'Active configuration');
assert.ok(elements['event-content'].textContent.includes('classification_count'));
assert.ok(elements['event-content'].textContent.includes('before rubric'));
handlers.select({items:['cycle-band:0']});
assert.equal(elements['cycle-states'].hidden,false);
assert.ok(elements['cycle-before'].textContent.includes('before rubric'));
assert.ok(elements['cycle-after'].textContent.includes('after rubric'));
const pointerPositions=[];
timeline.setCustomTime=point=>pointerPositions.push(+point);
handlers.select({items:[4]});
assert.equal(currentStep,ordered.findIndex(record=>record.key==='4'));
elements.play.onclick();assert.equal(position,4);tick();
assert.equal(position,5);assert.equal(pointerPositions.at(-1),Math.trunc(stepPositions.get('5')));
elements.play.onclick();
handlers.click({time:new Date(stepPositions.get('1')+2),item:null,what:'background'});
assert.equal(pointerPositions.at(-1),Math.trunc(stepPositions.get('1')+2));
elements.next.onclick();assert.equal(position,1);
elements.back.onclick();assert.equal(position,0);
handlers.select({items:[3]});
assert.equal(elements['paired-request'].hidden,false);
assert.equal(elements['decision-exchange'].hidden,false);
elements['paired-request'].onclick();assert.equal(position,1);
assert.ok(elements['event-content'].textContent.includes('examples'));
handlers.select({items:[2]});
assert.equal(elements['decision-exchange'].hidden,false);
assert.ok(elements['decision-request-content'].textContent.includes('examples'));
assert.ok(elements['decision-response-content'].textContent.includes('reject'));
assert.equal(elements['paired-request'].hidden,false);
elements['paired-request'].onclick();assert.equal(position,1);
assert.ok(elements['decision-response-content'].textContent.includes('reject'));
handlers.select({items:[4]});assert.equal(elements['decision-exchange'].hidden,true);
events.push({kind:'decision-request',target_id:'paper',cycle_id:'one',step_id:'retry',state:{examples:[{text:'different context',label:'accept'}]},questions:{}});
events.push({kind:'decision-response',target_id:'paper',cycle_id:'one',step_id:'retry',answers:{decision:{choice:'accept'}}});
assert.equal(decisionExchange(2).request,events[1]);
assert.equal(decisionExchange(7).request,events[6]);
assert.equal(decisionExchange(6).response,events[7]);
assert.equal(decisionExchange(1).response,events[2]);
events.pop();events.pop();
const metricFixture={kind:'candidate-evaluated',incumbent:{accuracy:.5,count:4,per_class:{accept:{recall:.5},reject:{recall:.5}}},candidate:{accuracy:.75,count:4,per_class:{accept:{recall:1},reject:{recall:.5}}},promoted:false};
const visualMetrics=evaluationMetrics(metricFixture);
assert.equal(visualMetrics.rows.find(row=>row.label==='Accuracy').candidate,.75);
assert.equal(visualMetrics.rows.find(row=>row.label==='Recall · accept').candidate,1);
assert.equal(visualMetrics.rows.find(row=>row.label==='Precision').candidate,null);
assert.equal(evaluationMetrics({kind:'prediction'}),null);
assert.equal(timeline.options.zoomMin,4);
assert.equal(elements['show-history'].hidden,true);
assert.ok(!timelineData.groups.some(group=>group.id==='decision-api'));
applyFilters();assert.ok(!timeline.items.some(item=>item.group==='decision-api'));
handlers.doubleClick({item:3});
assert.ok(timeline.window[1]-timeline.window[0]<=1000);
elements['show-run'].onclick();assert.deepEqual(timeline.window,[0,1000]);
timeline.setWindow(100,300);
const gesture=(dx,dy,extra={})=>({deltaX:dx,deltaY:dy,deltaMode:0,clientX:500,preventDefault(){},stopImmediatePropagation(){},...extra});
elements.timeline.wheel(gesture(100,10));
assert.deepEqual(timeline.window,[120,320]);
elements.timeline.wheel(gesture(-100,0));assert.deepEqual(timeline.window,[100,300]);
elements.timeline.wheel(gesture(100,10,{timeStamp:1000}));
elements.timeline.wheel(gesture(10,100,{timeStamp:1010}));
assert.equal(timeline.window[1]-timeline.window[0],200);
const beforeVertical=[...timeline.window];
let consumed=false;
elements.timeline.wheel(gesture(0,100,{preventDefault(){consumed=true;}}));
assert.deepEqual(timeline.window,beforeVertical);assert.equal(consumed,false);
assert.equal(timeline.options.height,'100%');assert.equal(timeline.options.verticalScroll,true);
elements.timeline.wheel(gesture(0,100,{preventDefault(){consumed=true;}}));
assert.deepEqual(timeline.window,beforeVertical);assert.equal(consumed,false);
elements.timeline.wheel(gesture(0,-100,{ctrlKey:true,preventDefault(){consumed=true;}}));
assert.ok(timeline.window[1]-timeline.window[0]<200);assert.equal(consumed,true);
const beforePinch=timeline.window[1]-timeline.window[0];
elements.timeline.wheel(gesture(10,100,{ctrlKey:true,timeStamp:1020}));
assert.ok(timeline.window[1]-timeline.window[0]>beforePinch);
timeline.setWindow(100,101);elements['zoom-out'].onclick();assert.ok(timeline.window[1]-timeline.window[0]>=4);
elements['zoom-out'].onclick();assert.ok(timeline.window[1]-timeline.window[0]>=8);
timeline.setWindow(0,4);elements['fit-all'].onclick();assert.deepEqual(timeline.window,[0,1000]);
elements['disagreement-filter'].checked=true;applyFilters();
assert.ok(timeline.items.some(i=>i.className.startsWith('marker-prediction')));
assert.ok(timeline.items.some(i=>i.className.startsWith('marker-human')));
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
    events.append({'event_id':12,'kind':'proposal-validated','step_id':'a','step_stage':'rubric','created_at':'2026-10-06T12:01:11Z',
                   'previous':{'rubric':''},'candidate':{'rubric':'prefer curated knowledge'}})
    events.extend([{'event_id':13+i,'kind':'trigger-evaluated','stage':'rubric','due':due,'created_at':f'2026-10-06T12:01:{12+i}Z'} for i,due in enumerate((False,True))])
    events.append({'event_id':15,'kind':'cycle-metrics','created_at':'2026-10-06T12:01:14Z','metrics':{'accuracy':.5,'count':2}})
    events.append({'event_id':16,'kind':'cycle-metrics','created_at':'2026-10-06T12:01:15Z','metrics':{'accuracy':.75,'count':4}})
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
global.vis={Timeline:class{constructor(c,i,g,o){this.options=o;this.groups=g;}on(name,fn){handlers[name]=fn;if(name==='select')select=fn;}redraw(){this.redrawn=true;}addCustomTime(){}setCustomTime(){}setCustomTimeTitle(){}setItems(items){this.items=items;}setWindow(a,b){this.window=[+a,+b];}getWindow(){return {start:0,end:1000}}moveTo(point){this.center=+point}fit(){}}};
global.CustomEvent=class{constructor(type,options){this.type=type;this.detail=options.detail;}};
global.window={addEventListener(){},dispatchEvent(){}};
Object.assign(document.getElementById('timeline'),{clientWidth:1000,querySelector(){return null;},style:{setProperty(){}}});
CODE
assert.equal(timelineItems.find(item=>item.event_index===12).iconName,'circle');
assert.equal(timelineItems.find(item=>item.event_index===13).iconName,'circle-play');
assert.ok(trendItems.every(item=>item.className.startsWith('metric-line')));
assert.ok(trendItems.some(item=>item.id==='metric-line:accuracy:15'));
const accuracyCurve=trendItems.find(item=>item.id==='metric-line:accuracy:15');
assert.ok(accuracyCurve.content['inner'+'HT'+'ML'].includes('class="metric-area"'));
assert.ok(accuracyCurve.content['inner'+'HT'+'ML'].includes('L100 58 L0 58 Z'));
handlers.select({items:['metric-line:accuracy:15']});assert.equal(position,15);
assert.ok(elements['event-content'].textContent.includes('accuracy'));
select({items:[1]});
assert.equal(elements['optimizer-exchange'].hidden,false);
assert.ok(elements['optimizer-request-content'].textContent.includes('prompt-a'));
assert.ok(elements['optimizer-response-content'].textContent.includes('reply-a'));
assert.equal(position,1);
assert.ok(elements['event-content'].textContent.includes('prompt-a'));
assert.ok(!elements['event-content'].textContent.includes('reply-a'));
assert.equal(elements['content-box'].open,true);
select({items:[2]});
assert.ok(elements['event-content'].textContent.includes('reply-a'));
assert.ok(elements['event-content'].textContent.includes('tool-a'));
assert.ok(!elements['event-content'].textContent.includes('reply-b'));
select({items:[11]});
assert.equal(elements['optimizer-proposal'].hidden,false);
assert.ok(elements['optimizer-proposal-content'].textContent.includes('prefer curated knowledge'));
assert.ok(elements['optimizer-proposal-status'].textContent.includes('not activation'));
assert.equal(elements['optimizer-exchange'].hidden,false);
assert.ok(elements['optimizer-exchange-summary'].textContent.includes('Request event 2'));
assert.ok(elements['optimizer-exchange-summary'].textContent.includes('response event 3'));
assert.ok(elements['optimizer-request-content'].textContent.includes('prompt-a'));
assert.ok(elements['optimizer-response-content'].textContent.includes('reply-a'));
assert.ok(elements['optimizer-response-content'].textContent.includes('tool-a'));
assert.ok(!elements['optimizer-request-content'].textContent.includes('prompt-b'));
select({items:[2]});
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
assert.equal(timeline.options.preferZoom,false);
assert.equal(timeline.options.selectable,true);
assert.equal(timeline.options.verticalScroll,true);
assert.equal(timeline.options.maxHeight,undefined);
assert.equal(timeline.options.horizontalScroll,false);
assert.equal(timeline.options.horizontalScrollKey,'shiftKey');
assert.equal(timeline.options.horizontalScrollInvert,true);
assert.equal(timeline.options.zoomKey,'ctrlKey');
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
assert.ok(timelineItems.every(i=>i.content.textContent.length<=1));
assert.ok(timelineItems.every(i=>i.type==='box'));
assert.ok(timelineItems.find(i=>i.source_index===0).className.startsWith('marker-human'));
assert.ok(timelineItems.find(i=>i.source_index===1).className.startsWith('marker-prediction'));
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
assert.equal(timeline.items.filter(i=>i.type==='box'&&!String(i.id).startsWith('metric:')).length,1);
assert.equal(timeline.items.find(i=>i.source_index===0).source_index,0);
assert.ok(timeline.items.some(i=>i.type==='background'&&String(i.id).startsWith('cycle-band:')));
assert.ok(!timeline.items.some(i=>i.group==='step-items'));
assert.ok(!timeline.groups.some(g=>g.id==='step-items'));
const optimization=timeline.groups.find(g=>g.id==='optimization');
assert.equal(optimization.content,'Optimization');
assert.equal(optimization.showNested,true);
assert.deepEqual(optimization.nestedGroups,['triggers','rubric','examples','questions','classifier','optimization-outcomes']);
assert.deepEqual(timeline.groups.find(g=>g.id==='classifier').nestedGroups,['classifier-attempts','fit']);
assert.deepEqual(timeline.groups.find(g=>g.id==='evaluation-trends').nestedGroups,['metric-recall','metric-precision','metric-accuracy']);
assert.deepEqual(timeline.groups.slice(-4).map(g=>g.id),['evaluation-trends','metric-recall','metric-precision','metric-accuracy']);
select({items:['source:0']});
assert.ok(elements['event-title'].textContent.includes('accept'));
assert.equal(new Set(timelineItems.map(i=>+i.start)).size,timelineItems.length);
assert.equal(readable({messages:[{role:'user',content:JSON.stringify({human_explanations:['knowledge bases'],feedback:[{text:'Title\\nAbstract'}]})}]}).includes('Title\\n'),true);
assert.ok(!readable({content:'line one\\nline two'}).includes('\\\\n'));
assert.ok(readable({content:'<script>untrusted</script>'}).includes('<script>untrusted</script>'));
'''.replace('DATA', json.dumps(data)).replace('HTML', json.dumps(html)).replace('CODE', code)
    completed = subprocess.run(['node'], input=harness, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
