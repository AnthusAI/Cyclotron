"""Execute viewer controls against a stub DOM, without browser or network access."""
import json
import re
import shutil
import subprocess

import pytest

from .trace_artifact import render_trace


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
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){},replaceChildren(){}}),createElement:()=>({textContent:'',style:{},append(){},setAttribute(){}})};
let select;const handlers={};
global.vis={Timeline:class{constructor(c,i,g,o){this.options=o;this.groups=g;}on(name,fn){handlers[name]=fn;if(name==='select')select=fn;}addCustomTime(){}setCustomTime(){}setItems(items){this.items=items;}setWindow(){}getWindow(){return {start:0,end:1000}}moveTo(point){this.center=+point}fit(){}zoomIn(){this.zoomed='in'}zoomOut(){this.zoomed='out'}}};
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
elements['label-filter'].value='accept';applyFilters();
assert.equal(timeline.items.filter(i=>i.group.startsWith('label-class-')).length,2);
assert.ok(timeline.items.filter(i=>i.group.startsWith('label-class-'))[0].title.includes('accept'));
elements['label-filter'].value='';elements['comment-filter'].checked=true;applyFilters();
assert.equal(timeline.items.filter(i=>i.group.startsWith('label-class-')).length,2);
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
assert.equal(timeline.options.horizontalScroll,true);
assert.equal(timeline.options.horizontalScrollKey,'shiftKey');
assert.equal(timeline.options.horizontalScrollInvert,true);
assert.equal(timeline.options.zoomKey,'ctrlKey');
assert.equal(timeline.options.zoomMin,1000);
assert.equal(typeof timeline.options.onInitialDrawComplete,'function');
assert.ok(HTML.includes('.vis-item.vis-line,.vis-item.vis-dot{display:none}'));
assert.ok(HTML.includes('id="inspector"'));
assert.ok(HTML.includes('.vis-panel.vis-background,.vis-axis{pointer-events:none}'));
assert.ok(!HTML.includes('}.vis-group{min-height:36px}'));
assert.ok(!HTML.includes('id="view-seek"'));
elements['zoom-in'].onclick();assert.equal(timeline.zoomed,'in');
elements['zoom-out'].onclick();assert.equal(timeline.zoomed,'out');
handlers.timechanged({id:'playback',time:new Date(stepPositions.get('1'))});assert.equal(position,1);
elements['next'].onclick();assert.equal(position,2);assert.equal(timeline.center,stepPositions.get('2'));
assert.ok(timeline.options.maxHeight<=420);
assert.ok(timeline.options.format.minorLabels(new Date(1000)).includes('2'));
assert.ok(timelineItems.every(i=>i.content.textContent.length===1));
assert.ok(timelineItems.every(i=>i.type==='box'));
assert.equal(timelineItems.find(i=>i.source_index===0).className,'marker-human');
assert.equal(timelineItems.find(i=>i.source_index===1).className,'marker-prediction');
assert.ok(timelineItems.some(i=>i.className==='marker-optimizer-request'));
assert.ok(timelineItems.some(i=>i.className==='marker-optimizer-response'));
assert.equal(ordered.length,timelineItems.length);
assert.equal(stepPositions.has('10'),false);
elements['disagreement-filter'].checked=true;applyFilters();
assert.equal(timeline.items.length,1);
assert.equal(timeline.items[0].source_index,0);
assert.equal(new Set(timelineItems.map(i=>+i.start)).size,timelineItems.length);
assert.equal(readable({messages:[{role:'user',content:JSON.stringify({human_explanations:['knowledge bases'],feedback:[{text:'Title\\nAbstract'}]})}]}).includes('Title\\n'),true);
assert.ok(!readable({content:'line one\\nline two'}).includes('\\\\n'));
assert.ok(readable({content:'<script>untrusted</script>'}).includes('<script>untrusted</script>'));
'''.replace('DATA', json.dumps(data)).replace('HTML', json.dumps(html)).replace('CODE', code)
    completed = subprocess.run(['node'], input=harness, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
