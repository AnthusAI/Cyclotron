import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
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
    if(query.includes('events('))return {events:[]}
    return {run:variables?.id==='live'?live:replay,currentItem:null,jobs:[]}
  })
})
afterEach(()=>{cleanup();vi.clearAllMocks()})

test('opening the workspace shows the existing optimization timeline instead of an empty landing page',async()=>{
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:replay.name})).toBeVisible()
  expect(screen.getByTitle('Run timeline and event inspector')).toHaveAttribute('src','/runs/replay/timeline?revision=0')
  expect(screen.queryByText('Local · GraphQL · SQLite')).toBeNull()
  expect(screen.getByText(/87 cycles · 87 labels · 11 optimizer calls/)).toBeVisible()
})

test('reloading preserves an interactive run and its labeling view without creating or restarting it',async()=>{
  window.history.replaceState(null,'','#run=live&view=label')
  render(<WebApp />)
  expect(await screen.findByRole('heading',{name:live.name})).toBeVisible()
  expect(screen.getByRole('button',{name:'Prepare next item'})).toBeVisible()
  await waitFor(()=>expect(subscribeEvents).toHaveBeenCalledWith('live',0,expect.any(Function),expect.any(Function)))
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('the timeline shows loading feedback instead of a blank panel until it has loaded',()=>{
  render(<RunTimeline runId="replay" revision={0} />)
  expect(screen.getByText('Loading timeline…')).toBeVisible()
  fireEvent.load(screen.getByTitle('Run timeline and event inspector'))
  expect(screen.queryByText('Loading timeline…')).toBeNull()
})
