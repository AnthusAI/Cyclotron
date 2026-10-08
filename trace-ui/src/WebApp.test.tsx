import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {RunTimeline,WebApp} from './WebApp'
import {graphql,subscribeEvents} from './graphql'

vi.mock('./graphql',()=>({graphql:vi.fn(),subscribeEvents:vi.fn(()=>()=>{})}))
const live={id:'live',name:'Interactive study',mode:'live',status:'ready',createdAt:'2026-10-06',config:{selection_policy:{primary:'f1'}},counts:{cycles:1,predictions:1,labels:0,optimizations:0}}
const replay={id:'replay',name:'Existing optimization replay',mode:'recorded',status:'completed',createdAt:'2026-10-06',config:{},counts:{cycles:87,predictions:87,labels:87,optimizations:11}}
beforeEach(()=>{
  window.history.replaceState(null,'','/')
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[live,replay],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[],scorecardDefinitions:[]}
    if(query.includes('events('))return {events:[]}
    return {run:variables?.id==='live'?live:replay,currentItem:null,jobs:[]}
  })
})
afterEach(()=>{cleanup();sessionStorage.clear();vi.clearAllMocks()})

function classifiedRunFixture(){
  const card={...live,config:{classifiers:[
    {id:'a',name:'Relevance',config:{classes:[{label:'yes'},{label:'no'}]}},
    {id:'b',name:'Practicality',config:{classes:[{label:'yes'},{label:'no'}]}},
  ]}}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[card],capabilities:{liveEnabled:true,itemCount:1}}
    if(query.includes('events('))return {events:[]}
    return {run:card,currentItem:null,jobs:[]}
  })
}

test('moving to a valid run clears an old load error without submitting commands',async()=>{
  const missing={...live,id:'missing',name:'Unavailable run'}
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[missing,live],capabilities:{liveEnabled:true,itemCount:1}}
    if(variables?.id==='missing')throw new Error('unknown run')
    if(query.includes('events('))return {events:[]}
    return {run:live,currentItem:null,jobs:[]}
  })
  window.history.replaceState(null,'','#run=missing&view=label&section=optimizations')
  render(<WebApp/> )
  expect(await screen.findByText('unknown run')).toBeVisible()
  window.history.replaceState(null,'','#run=live&view=label&section=optimizations')
  fireEvent(window,new PopStateEvent('popstate'))
  await screen.findByRole('heading',{name:'Interactive study'})
  expect(screen.queryByText('unknown run')).toBeNull()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('labeling exposes recorded optimizer completion and opens the full correlated exchange',async()=>{
  const card={...live,config:{classifiers:[{id:'a',name:'Relevance',config:{classes:[{label:'yes'},{label:'no'}]}}]}}
  const scope={classifier_id:'a',step_id:'rubric-one',step_stage:'rubric'}
  const history=[
    {sequence:1,sourceId:'request',payload:{...scope,kind:'optimizer-request',messages:[{role:'user',content:'The human explanation must be retained in this optimizer context'}]}},
    {sequence:2,sourceId:'response',payload:{...scope,kind:'optimizer-response',content:'Prefer knowledge-base research'}},
    {sequence:3,sourceId:'completed',payload:{...scope,kind:'step-completed',status:'completed',result:{activated:true}}},
  ]
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[card],capabilities:{liveEnabled:true,itemCount:1}}
    if(query.includes('events('))return {events:variables?.after===0?history:[]}
    return {run:card,currentItem:null,jobs:[]}
  })
  window.history.replaceState(null,'','#run=live&view=label&section=optimizations')
  render(<WebApp/> )
  expect(await screen.findByText('Accepted')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Inspect latest optimization event'}))
  expect(screen.getByText('The human explanation must be retained in this optimizer context')).toBeVisible()
  expect(screen.getByText('Prefer knowledge-base research')).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})

test('a classifier-specific deep link restores the matching history on initial load',async()=>{
  classifiedRunFixture()
  window.history.replaceState(null,'','#run=live&view=timeline&section=optimizations&classifier=b')
  render(<WebApp/> )
  expect(await screen.findByRole('combobox',{name:'Inspect classifier'})).toHaveValue('b')
  expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/live/timeline?revision=0&classifier_id=b')
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})

test('changing the inspected classifier survives a remount and changing views',async()=>{
  classifiedRunFixture()
  window.history.replaceState(null,'','#run=live&view=timeline&section=optimizations')
  render(<WebApp/> )
  fireEvent.change(await screen.findByRole('combobox',{name:'Inspect classifier'}),{target:{value:'b'}})
  await waitFor(()=>expect(new URLSearchParams(window.location.hash.slice(1)).get('classifier')).toBe('b'))
  cleanup()
  render(<WebApp/> )
  expect(await screen.findByRole('combobox',{name:'Inspect classifier'})).toHaveValue('b')
  fireEvent.click(screen.getByRole('button',{name:'Label items'}))
  await screen.findByRole('button',{name:'Prepare next item'})
  fireEvent.click(screen.getByRole('button',{name:'Timeline'}))
  await waitFor(()=>expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/live/timeline?revision=0&classifier_id=b'))
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})

test('browser navigation restores the first or another classifier without pushing a new route',async()=>{
  classifiedRunFixture()
  window.history.replaceState(null,'','#run=live&view=timeline&section=optimizations&classifier=b')
  render(<WebApp/> )
  await screen.findByRole('combobox',{name:'Inspect classifier'})
  for(const classifier of ['a','b']){
    window.history.replaceState(null,'',`#run=live&view=timeline&section=optimizations&classifier=${classifier}`)
    const push=vi.spyOn(window.history,'pushState')
    fireEvent(window,new PopStateEvent('popstate'))
    await waitFor(()=>expect(screen.getByRole('combobox',{name:'Inspect classifier'})).toHaveValue(classifier))
    expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src',`/runs/live/timeline?revision=0&classifier_id=${classifier}`)
    expect(push).not.toHaveBeenCalled()
    push.mockRestore()
  }
})

test('an unknown classifier link is replaced with a valid default without loading unrelated history',async()=>{
  classifiedRunFixture()
  window.history.replaceState(null,'','#run=live&view=timeline&section=optimizations&classifier=other-run-classifier')
  const push=vi.spyOn(window.history,'pushState')
  render(<WebApp/> )
  expect(await screen.findByRole('combobox',{name:'Inspect classifier'})).toHaveValue('a')
  await waitFor(()=>expect(new URLSearchParams(window.location.hash.slice(1)).has('classifier')).toBe(false))
  expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/live/timeline?revision=0&classifier_id=a')
  expect(push).not.toHaveBeenCalled()
  push.mockRestore()
  fireEvent.click(screen.getByRole('button',{name:'Label items'}))
  await screen.findByRole('button',{name:'Prepare next item'})
  expect(new URLSearchParams(window.location.hash.slice(1)).has('classifier')).toBe(false)
})

test('scorecard feedback recovery uses the original job through its dedicated API mutation',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  const card={...live,config:{classifiers:[{id:'a',name:'Relevance',config:{classes:[{label:'yes'},{label:'no'}]}}]}}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[card],capabilities:{liveEnabled:true,itemCount:1}}
    if(query.includes('events('))return {events:[]}
    if(query.includes('resumeFeedbackCommand'))return {resumeFeedbackCommand:{id:'original-job',kind:'undo',status:'pending',result:null}}
    return {run:card,currentItem:null,jobs:[{id:'original-job',kind:'undo',status:'failed',result:null}]}
  })
  render(<WebApp/> )
  fireEvent.click(await screen.findByRole('button',{name:'Recover feedback command'}))
  await waitFor(()=>expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('resumeFeedbackCommand'))).toBe(true))
  expect(vi.mocked(graphql).mock.calls.find(([q])=>q.includes('resumeFeedbackCommand'))?.[1]).toEqual({id:'live',job:'original-job'})
  expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('submitCommand'))).toBe(false)
})

test('new scorecard runs inherit their objective unless the user explicitly overrides it',async()=>{
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[{id:'items',name:'Items',count:3}],scorecardDefinitions:[{id:'card',name:'Card',revision:2}]}
    if(query.includes('events('))return {events:[]}
    if(query.includes('createRun'))return {createRun:{...live,id:'created'}}
    return {run:live,currentItem:null,jobs:[]}
  })
  render(<WebApp/> )
  fireEvent.click(await screen.findByRole('button',{name:'Run history'}))
  fireEvent.click(screen.getByRole('button',{name:'New run'}))
  const setup=screen.getByRole('dialog',{name:'New optimization run'})
  await waitFor(()=>expect(within(setup).getByLabelText('Scorecard')).toHaveValue('card'))
  expect(within(setup).getByLabelText('Objective')).toHaveValue('inherit')
  fireEvent.change(within(setup).getByLabelText('Name'),{target:{value:'Inherited defaults'}})
  fireEvent.click(within(setup).getByRole('checkbox',{name:'Authorize paid calls within these limits'}))
  fireEvent.click(within(setup).getByRole('button',{name:'Create run'}))
  await waitFor(()=>expect(vi.mocked(graphql).mock.calls.some(([query])=>query.includes('createRun'))).toBe(true))
  const variables=vi.mocked(graphql).mock.calls.find(([query])=>query.includes('createRun'))![1]
  expect(variables?.config).toMatchObject({scorecard_id:'card'})
  expect(variables?.config).not.toHaveProperty('selection_policy')
})

test('new run setup is a dismissible drawer and preserves unsent labeling feedback',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  const shown={item:{id:'paper',values:{title:'A paper'}},prediction:{presentation_id:'shown',classifiers:{a:{name:'Topic',label:'include',confidence:.8,classes:['include','exclude']}}}}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[],scorecardDefinitions:[]}
    if(query.includes('events('))return {events:[]}
    return {run:live,currentItem:shown,jobs:[]}
  })
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Topic: exclude'}))
  fireEvent.click(screen.getByRole('button',{name:'Run history'}))
  fireEvent.click(screen.getByRole('button',{name:'New run'}))
  const setup=screen.getByRole('dialog',{name:'New optimization run'})
  expect(within(setup).getByLabelText('Session type')).toBeVisible()
  expect(within(setup).getByRole('button',{name:'Create run'})).toBeDisabled()
  fireEvent.click(within(setup).getByRole('button',{name:'Close New optimization run'}))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(screen.getByRole('button',{name:'Topic: exclude'})).toHaveAttribute('aria-pressed','true')
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('inspecting a replay and returning through browser history restores unsent labels and explanations',async()=>{
  window.history.replaceState(null,'','#run=live&view=label&section=optimizations')
  const shown={item:{id:'paper',values:{title:'A paper'}},prediction:{presentation_id:'unsent',classifiers:{a:{name:'Topic',label:'include',confidence:.8,classes:['include','exclude']}}}}
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[live,replay],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[],scorecardDefinitions:[]}
    if(query.includes('events('))return {events:[]}
    return {run:variables?.id==='live'?live:replay,currentItem:variables?.id==='live'?shown:null,jobs:[]}
  })
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Topic: exclude'}))
  fireEvent.change(screen.getByLabelText('Explanation for Topic'),{target:{value:'The original human explanation'}})
  fireEvent.click(screen.getByRole('button',{name:'Run history'}))
  fireEvent.click(screen.getByRole('button',{name:/Existing optimization replay.*cycles/}))
  expect(await screen.findByRole('heading',{name:replay.name})).toBeVisible()
  expect(screen.queryByLabelText('Explanation for Topic')).toBeNull()
  window.history.replaceState(null,'','#run=live&view=label&section=optimizations')
  fireEvent(window,new PopStateEvent('popstate'))
  expect(await screen.findByRole('button',{name:'Topic: exclude'})).toHaveAttribute('aria-pressed','true')
  expect(screen.getByLabelText('Explanation for Topic')).toHaveValue('The original human explanation')
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('opening the workspace shows the existing optimization timeline instead of an empty landing page',async()=>{
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:replay.name})).toBeVisible()
  expect(screen.queryByText('No recorded cycles yet.')).toBeNull()
  expect(screen.queryByLabelText('Cyclotron run timeline')).toBeNull()
  expect(screen.getByText('Cyclotron')).toHaveClass('cyclotron-brand')
  expect(screen.getByText('SELF-ALIGNING DECISION MODEL HARNESS')).toHaveClass('cyclotron-tagline')
  const title=screen.getByText('Cyclotron')
  const logo=title.parentElement?.querySelector('svg')
  expect(logo).toHaveAttribute('viewBox','0 0 72 24')
  expect(logo).toHaveAttribute('stroke','currentColor')
  expect(title.parentElement?.firstElementChild).toBe(title)
  expect(screen.getByRole('banner').querySelector('.lucide-refresh-cw')).toBeNull()
  expect(screen.queryByText('Decision Flywheel')).toBeNull()
  expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/replay/timeline?revision=0')
  expect(screen.queryByText('Local · GraphQL · SQLite')).toBeNull()
  expect(screen.getByText(/87 cycles · 87 labels · 11 optimizer calls/)).toBeVisible()
})

test('a selected run keeps the workspace shape with a skeleton while its details load',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  let resolveDetails:(value:unknown)=>void=()=>{}
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('events('))return {events:[]}
    if(query.includes('run(runId:$id)'))return new Promise(resolve=>{resolveDetails=resolve})
    return {run:variables?.id==='live'?live:replay,currentItem:null,jobs:[]}
  })
  render(<WebApp />)
  expect(await screen.findByTestId('run-workspace-skeleton')).toBeVisible()
  expect(screen.queryByText('Loading run…')).toBeNull()
  resolveDetails({run:live,currentItem:null,jobs:[]})
  expect(await screen.findByRole('heading',{name:live.name})).toBeVisible()
})

test('reloading preserves an interactive run and its labeling view without creating or restarting it',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:live.name})).toBeVisible()
  expect(screen.getByRole('button',{name:'Prepare next item'})).toBeVisible()
  await waitFor(()=>expect(subscribeEvents).toHaveBeenCalledWith('live',0,expect.any(Function),expect.any(Function)))
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('scorecard navigation has its own route and responds to browser history changes',async()=>{
  window.history.replaceState(null,'','#run=live&view=label&section=classifiers')
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('scorecardDefinitions'))return {scorecardDefinitions:[],classifiers:[]}
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('events('))return {events:[]}
    return {run:live,currentItem:null,jobs:[]}
  })
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:'Scorecards'})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Optimizations'}))
  expect(await screen.findByRole('heading',{name:live.name})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Scorecards'}))
  expect(window.location.hash).toContain('section=scorecards')
  window.history.replaceState(null,'','#run=live&view=label&section=optimizations')
  fireEvent(window,new PopStateEvent('popstate'))
  expect(await screen.findByRole('heading',{name:live.name})).toBeVisible()
})

test('the narrow navigation uses a descriptive full-viewport menu instead of a select menu',async()=>{
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('{classifiers itemLists}'))return {classifiers:[],itemLists:[]}
    if(query.includes('capabilities'))return {runs:[live,replay],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[],scorecardDefinitions:[]}
    if(query.includes('events('))return {events:[]}
    return {run:variables?.id==='live'?live:replay,currentItem:null,jobs:[]}
  })
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Open main menu'}))
  const menu=screen.getByRole('dialog',{name:'Navigate Cyclotron'})
  expect(document.body).toHaveAttribute('data-scroll-locked')
  expect(within(menu).getByText('Cyclotron')).toBeVisible()
  expect(within(menu).getByText('SELF-ALIGNING DECISION MODEL HARNESS')).toBeVisible()
  expect(within(menu).getByRole('button',{name:'Close main menu'})).toBeVisible()
  expect(screen.queryByRole('navigation',{name:'Main navigation'})).toBeNull()
  expect(within(menu).getByText('Define the related classifiers that share one decision-model request.')).toBeVisible()
  expect(within(menu).getByText('Browse source items, decisions, and human labels across scorecards.')).toBeVisible()
  expect(within(menu).getByText('Run, compare, and inspect live or replayed flywheel experiments.')).toBeVisible()
  expect(screen.queryByRole('combobox',{name:'Navigation'})).toBeNull()
  fireEvent.click(within(menu).getByRole('button',{name:/Item lists/}))
  await waitFor(()=>expect(window.location.hash).toContain('section=items'))
  expect(screen.queryByRole('dialog',{name:'Navigate Cyclotron'})).toBeNull()
})

test('Scenario: A labeler can compare a displayed prediction with a human label',async()=>{
  const first={item:{id:'first',title:'First paper',abstract:'First abstract',submitted_at:'2026-10-06',categories:['cs.AI'],authors:'A. Author'},prediction:{label:'exclude',confidence:.8,presentation_id:'first-presentation'}}
  const next={item:{id:'next',title:'Next paper',abstract:'Next abstract',submitted_at:'2026-10-07',categories:['cs.AI'],authors:'B. Author'},prediction:{label:'include',confidence:.7,presentation_id:'next-presentation'}}
  let submitted=false
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('events('))return {events:[]}
    if(query.includes('submitCommand')){
      submitted=true
      return {submitCommand:{id:'label-job',kind:'label',status:'running',result:null}}
    }
    return {run:live,currentItem:submitted?next:first,jobs:submitted?[]:[]}
  })
  window.history.replaceState(null,'','#run=live&view=label')
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:'Interactive study'})).toBeVisible()
  expect(await screen.findByText('First paper')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Include'}))
  expect(vi.mocked(graphql)).toHaveBeenCalledWith(expect.stringContaining('submitCommand'),expect.objectContaining({
    id:'live',kind:'label',payload:expect.objectContaining({item_id:'first',label:'include',presentation_id:'first-presentation'}),
  }))
  await waitFor(()=>expect(screen.getByText('Next paper')).toBeVisible(),{timeout:2000})
})

test('the timeline shows loading feedback instead of a blank panel until it has loaded',()=>{
  render(<RunTimeline runId="replay" revision={0} />)
  expect(screen.getByText('Loading timeline…')).toBeVisible()
  fireEvent.load(screen.getByTitle('Run timeline and event inspector'))
  expect(screen.queryByText('Loading timeline…')).toBeNull()
})

test('run history filters by name without changing the active session',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Run history'}))
  fireEvent.change(screen.getByLabelText('Search run history'),{target:{value:'Existing'}})
  expect(screen.queryByRole('button',{name:/Interactive study.*cycles/})).toBeNull()
  expect(screen.getByRole('button',{name:/Existing optimization replay.*cycles/})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Close Run history'}))
  expect(screen.getByRole('heading',{name:live.name})).toBeVisible()
})

test('a replay history entry opens its timeline and completed replay cycles refresh it automatically',async()=>{
  const fresh={...live,id:'fresh',name:'Fresh replay',config:{input_mode:'replay'}}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[fresh],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('eventCursor'))return {run:{eventCursor:12}}
    if(query.includes('events('))return {events:[]}
    return {run:fresh,currentItem:null,jobs:[{id:'step',kind:'replay-next',status:'completed',result:{finished:false}}]}
  })
  render(<WebApp />)
  await screen.findByRole('heading',{name:'Fresh replay'})
  await waitFor(()=>expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/fresh/timeline?revision=step'))
  fireEvent.click(screen.getByRole('button',{name:'Run history'}))
  fireEvent.click(screen.getByRole('button',{name:/Fresh replay.*Replay/}))
  expect(screen.getByTitle('Run timeline and event inspector')).toBeInTheDocument()
})

test('label submission responds immediately and does not claim queued votes are already recorded',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  let acknowledge:(value:unknown)=>void=()=>{}
  const shown={item:{id:'paper',values:{title:'A paper'}},prediction:{presentation_id:'shown',classifiers:{a:{name:'Topic',label:'include',confidence:.8,classes:['include','exclude']}}}}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('events('))return {events:[]}
    if(query.includes('submitCommand'))return new Promise(resolve=>{acknowledge=resolve})
    return {run:live,currentItem:shown,jobs:[]}
  })
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Topic: exclude'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit feedback'}))
  expect(screen.getByText('Sending 1 label…')).toBeVisible()
  expect(screen.getByRole('button',{name:'Submitting…'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Topic: exclude'})).toBeDisabled()
  acknowledge({submitCommand:{id:'vote',kind:'label',status:'pending',result:null}})
  expect(await screen.findByText('1 label received · waiting to record')).toBeVisible()
  expect(screen.queryByText(/Labels recorded/)).toBeNull()
})

test('retrying an unconfirmed label after remount recovers the original command identity',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  const shown={item:{id:'paper',values:{title:'A paper'}},prediction:{presentation_id:'shown',classifiers:{a:{name:'Topic',label:'include',confidence:.8,classes:['include','exclude']}}}}
  let attempts=0
  vi.mocked(graphql).mockImplementation(async query=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:1}}
    if(query.includes('events('))return {events:[]}
    if(query.includes('submitCommand')){
      if(++attempts===1)throw new Error('Acknowledgement lost')
      return {submitCommand:{id:'same-job',kind:'label',status:'pending',result:null}}
    }
    return {run:live,currentItem:shown,jobs:[]}
  })
  const first=render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Topic: exclude'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit feedback'}))
  await screen.findByText('Label submission needs attention')
  first.unmount()
  render(<WebApp />)
  fireEvent.click(await screen.findByRole('button',{name:'Submit feedback'}))
  await screen.findByText('1 label received · waiting to record')
  const submissions=vi.mocked(graphql).mock.calls.filter(([query])=>query.includes('submitCommand'))
  expect(submissions).toHaveLength(2)
  expect(submissions[1][1]).toEqual(submissions[0][1])
})

test('labeling uses the focused app layout with history and activity drawers without restarting',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  render(<WebApp />)
  expect(await screen.findByRole('button',{name:'Run history'})).toBeVisible()
  expect(document.querySelector('[data-labeling-view="true"]')).not.toBeNull()
  expect(screen.queryByRole('button',{name:'Full-screen labeling'})).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Optimizer activity'}))
  expect(screen.getByRole('dialog',{name:'Optimizer activity'})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Close Optimizer activity'}))
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('the activity drawer can isolate optimizer exchanges from later decision traffic',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  const request={sequence:1,sourceId:'1',payload:{kind:'optimizer-request',classifier_id:'a',step_id:'s',briefing_fingerprint:'f',messages:[{role:'user',content:'Explain knowledge-base feedback'}]}}
  const response={sequence:2,sourceId:'2',payload:{kind:'optimizer-response',classifier_id:'a',step_id:'s',briefing_fingerprint:'f',content:'Keep knowledge-base articles'}}
  const decisions=Array.from({length:45},(_,i)=>({sequence:i+3,sourceId:String(i+3),payload:{kind:'decision-response'}}))
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('capabilities'))return {runs:[live],capabilities:{liveEnabled:true,itemCount:250}}
    if(query.includes('scorecardDefinitions'))return {classifiers:[],itemLists:[],scorecardDefinitions:[]}
    if(query.includes('events('))return {events:variables?.after?[]:[request,response,...decisions]}
    return {run:live,currentItem:null,jobs:[]}
  })
  render(<WebApp/> )
  fireEvent.click(await screen.findByRole('button',{name:'Optimizer activity'}))
  fireEvent.change(screen.getByLabelText('Activity type'),{target:{value:'optimizer'}})
  fireEvent.click(await screen.findByRole('button',{name:/optimizer response/i}))
  expect(screen.getByText('Explain knowledge-base feedback')).toBeVisible()
  expect(screen.getByText('Keep knowledge-base articles')).toBeVisible()
})
