"""Execute viewer controls against a stub DOM, without browser or network access."""
import json
import re
import shutil
import subprocess

import pytest

from .trace_artifact import render_trace


def test_clicking_round_start_shows_its_future_response_and_never_another_rounds_reply():
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
    html = render_trace(events)
    data = dict(re.findall(r'<script id="([^"]+)" type="application/json">(.*?)</script>', html, re.S))
    code = re.findall(r'<script>(.*?)</script>', html, re.S)[-1]
    harness = r'''
const assert=require('node:assert/strict');
const elements={};
for(const [id,text] of Object.entries(DATA))elements[id]={textContent:text};
global.document={getElementById:id=>elements[id]||(elements[id]={textContent:'',value:'',append(){}}),createElement:()=>({textContent:''})};
let select;
global.vis={Timeline:class{on(name,fn){select=fn}addCustomTime(){}setCustomTime(){}}};
CODE
select({items:[0]});
assert.equal(position,0);
assert.ok(elements['round-responses'].textContent.includes('reply-a'));
assert.ok(elements['round-responses'].textContent.includes('tool-a'));
assert.ok(!elements['round-responses'].textContent.includes('reply-b'));
assert.ok(elements['round-requests'].textContent.includes('prompt-a'));
assert.ok(elements['round-outcome'].textContent.includes('outcome-a'));
elements['round'].value='4';elements['round'].onchange();
assert.ok(elements['round-responses'].textContent.includes('reply-b'));
assert.equal(readable({messages:[{role:'user',content:JSON.stringify({human_explanations:['knowledge bases'],feedback:[{text:'Title\\nAbstract'}]})}]}).includes('Title\\n'),true);
assert.ok(!readable({content:'line one\\nline two'}).includes('\\\\n'));
assert.ok(readable({content:'<script>untrusted</script>'}).includes('<script>untrusted</script>'));
'''.replace('DATA', json.dumps(data)).replace('CODE', code)
    completed = subprocess.run(['node', '-e', harness], capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
