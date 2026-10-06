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
    history=[{'source_table':'review_events','record':{'label':'accept','action':'vote','comment':'why','created_at':'2026-10-06T12:02:00Z'},
              'article':{'title':'Source title','abstract':'Source abstract','assignment':'train'},'presentation':{'predicted_label':'reject','confidence':.9}}]
    html = render_trace(events, history)
    data = dict(re.findall(r'<script id="([^"]+)" type="application/json">(.*?)</script>', html, re.S))
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    harness = r'''
const assert=require('node:assert/strict');
const elements={};
for(const [id,text] of Object.entries(DATA))elements[id]={textContent:text};
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){},replaceChildren(){}}),createElement:()=>({textContent:'',append(){}})};
let select;
global.vis={Timeline:class{on(name,fn){select=fn}addCustomTime(){}setCustomTime(){}setItems(items){this.items=items;}setWindow(){}fit(){}}};
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
assert.equal(timeline.items.filter(i=>i.group==='feedback').length,2);
assert.ok(timeline.items.filter(i=>i.group==='feedback')[0].content.textContent.includes('accept'));
elements['label-filter'].value='';elements['comment-filter'].checked=true;applyFilters();
assert.equal(timeline.items.filter(i=>i.group==='feedback').length,2);
assert.equal(elements['content-box'].open,false);
select({items:['source:0']});
assert.ok(elements['event-title'].textContent.includes('accept'));
assert.ok(elements['event-content'].textContent.includes('Source abstract'));
assert.ok(elements['event-content'].textContent.includes('reject'));
assert.equal(readable({messages:[{role:'user',content:JSON.stringify({human_explanations:['knowledge bases'],feedback:[{text:'Title\\nAbstract'}]})}]}).includes('Title\\n'),true);
assert.ok(!readable({content:'line one\\nline two'}).includes('\\\\n'));
assert.ok(readable({content:'<script>untrusted</script>'}).includes('<script>untrusted</script>'));
'''.replace('DATA', json.dumps(data)).replace('CODE', code)
    completed = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
