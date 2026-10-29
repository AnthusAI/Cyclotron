import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {LabDecisions} from './LabDecisions'
import {WebApp} from './WebApp'
import {WorkspaceTransportProvider} from './WorkspaceTransport'
import type {DecisionRow,DecisionsPage,Health,ItemDetail,StandingRun} from './labApi'

const RUN:StandingRun={runId:'run-1',name:'Anthus combined',cyclotron:{id:'shared',revision:1},bindings:[{id:'anth-us',outputs:['anth-us']},{id:'shipped',outputs:['shipped']}],
  outputs:[{id:'anth-us',name:'Anthus',question:'Relevant?',classes:['include','exclude']},{id:'shipped',name:'Shipped',question:'Shipped?',classes:['include','exclude']}]}
const review={selected:true,reason:'program',propensity:1,detail:'Sampled at the 100% review rate.'}
const row=(n:number,extra:Partial<DecisionRow>={}):DecisionRow=>({position:n,jobId:`job-${n}`,decisionId:`decision-${n}`,decidedAt:`2026-10-28T10:00:0${n}Z`,
  item:{id:`story-${n}`,revision:1,fingerprint:'f',title:`Story ${n}`,source:'feed-a'},
  outputs:{'anth-us':{binding:'anth-us',label:'include',confidence:.8,probabilities:{include:.8,exclude:.2},version:1,review,humanLabel:null},
    shipped:{binding:'shipped',label:'exclude',confidence:.6,probabilities:{include:.4,exclude:.6},version:1,review:{...review,selected:false},humanLabel:'exclude'}},...extra})
const HEALTH:Health={runId:'run-1',generatedAt:'2026-10-28T10:00:10Z',decisions:{lastHour:42,lastDay:2035},secondsBetweenDecisions:{window:'last hour',samples:41,median:4.2,p90:7.9},queue:{pending:3,running:1},failed:{lastDay:2},latestPosition:4}
const DETAIL:ItemDetail={runId:'run-1',item:{id:'story-4',revision:2,values:{title:'Story 4',url:'https://example.test/story-4',summary:'A short summary.',text:'The full text.',source:'feed-a'}},
  revisions:[{revision:1,fingerprint:'a',occurredAt:null,importedAt:'2026-10-27T10:00:00Z',values:{title:'Story 4',text:'Landing page'},provenance:null},
    {revision:2,fingerprint:'b',occurredAt:null,importedAt:'2026-10-28T10:00:00Z',values:{title:'Story 4',text:'The full text.'},provenance:{kind:'machine',principal:'arn:aws:iam::1:role/papyrus',session:'papyrus-monitor',requestId:'machine:req-1',jobId:'job-4'}}],
  decision:{jobId:'job-4',position:4,decisionId:'decision-4',decidedAt:'2026-10-28T10:00:04Z',itemRevision:2,outputs:{
    'anth-us':{...row(4).outputs['anth-us'],name:'Anthus',model:'jev-1.13.0',composition:{examples_shown:12,examples_available:40,target_trimmed:true},prediction:null,exchange:'shared-1'},
    shipped:{...row(4).outputs.shipped,name:'Shipped',model:'jev-1.13.0',composition:null,prediction:null,exchange:'shared-1'}}},
  exchanges:{'shared-1':{request:{kind:'decision-request',event_id:7,state:{target:{text:'Exact request target text'}}},response:{kind:'decision-response',event_id:8,answers:{q:'Exact model answer'}},outputs:['anth-us','shipped']}},
  labels:[{output:'shipped',classifierRevision:1,itemRevision:1,label:'exclude',comment:'Not shipped yet.',createdAt:'2026-10-27T11:00:00Z',retracted:false,requestId:'machine-feedback:x',source:{kind:'caller',binding:'shipped',caller:'shipped',reviewer:'editor-17',asserted:true,principal:'arn'}}]}

let pages:Record<string,DecisionsPage>,calls:string[]
function respond(body:unknown,status=200){return new Response(JSON.stringify(body),{status,headers:{'Content-Type':'application/json'}})}
beforeEach(()=>{
  calls=[]
  pages={
    '':{runId:'run-1',decisions:[row(4),row(3)],examined:{from:4,to:3},next:'3',total:4},
    '3':{runId:'run-1',decisions:[row(2),row(1)],examined:{from:2,to:1},next:null,total:4},
  }
  vi.stubGlobal('fetch',vi.fn(async(input:string,init?:RequestInit)=>{
    calls.push(input)
    if(input==='/api/session')return respond({authenticated:true,selectedAccountId:'account-1'})
    expect((init?.headers as Record<string,string>)['X-Cyclotron-Account']).toBe('account-1')
    const url=new URL(input,'https://lab.test')
    if(url.pathname==='/api/lab/standing-runs')return respond([RUN])
    if(url.pathname==='/api/lab/runs/run-1/health')return respond(HEALTH)
    if(url.pathname==='/api/lab/runs/run-1/decisions')return respond(pages[url.searchParams.get('before')??''])
    if(url.pathname==='/api/lab/runs/run-1/items/story-4'&&url.searchParams.get('job')==='job-4')return respond(DETAIL)
    return respond({detail:'unexpected'},404)
  }))
})
afterEach(()=>{cleanup();vi.unstubAllGlobals();window.history.replaceState(null,'','/')})

test('the decisions view shows health tiles and the newest decisions with every output, routing and label status',async()=>{
  render(<LabDecisions/>)
  const list=await screen.findByRole('list',{name:'Decisions'})
  await waitFor(()=>expect(within(list).getAllByRole('listitem')).toHaveLength(2))
  expect(within(list).getAllByRole('listitem').map(item=>within(item).getByText(/^Story/).textContent)).toEqual(['Story 4','Story 3'])
  const first=within(list).getAllByRole('listitem')[0]
  expect(within(first).getByText('Anthus')).toBeVisible()
  expect(within(first).getByText('80%')).toBeVisible()
  expect(within(first).getByLabelText('Needs review')).toBeInTheDocument()
  expect(within(first).getByLabelText('Labeled exclude')).toBeInTheDocument()
  const health=screen.getByRole('region',{name:'Run health'})
  expect(within(health).getByText('42')).toBeVisible()
  expect(within(health).getByText('2035')).toBeVisible()
  expect(within(health).getByText('4.2 s')).toBeVisible()
  expect(within(health).getByText('3 waiting · 1 running')).toBeVisible()
  expect(within(health).getAllByTestId('health-tile')).toHaveLength(6)
})

test('loading more follows the server cursor until the oldest decision, each page once',async()=>{
  render(<LabDecisions/>)
  fireEvent.click(await screen.findByRole('button',{name:'Load more'}))
  const list=screen.getByRole('list',{name:'Decisions'})
  await waitFor(()=>expect(within(list).getAllByRole('listitem')).toHaveLength(4))
  expect(screen.queryByRole('button',{name:'Load more'})).toBeNull()
  expect(screen.getByRole('status')).toHaveTextContent('4 shown · searched 4 of 4 jobs · end of the run')
  expect(calls.filter(url=>url.includes('/decisions')).map(url=>new URL(url,'https://lab.test').searchParams.get('before'))).toEqual([null,'3'])
})

test('filters are sent to the server and reload the list from the newest decision',async()=>{
  render(<LabDecisions/>)
  await screen.findByRole('list',{name:'Decisions'})
  fireEvent.click(screen.getByRole('button',{name:'Filters'}))
  fireEvent.change(screen.getByLabelText('Output'),{target:{value:'shipped'}})
  fireEvent.change(screen.getByLabelText('Label'),{target:{value:'exclude'}})
  fireEvent.change(screen.getByLabelText('Human label'),{target:{value:'false'}})
  fireEvent.change(screen.getByLabelText('Confidence from (%)'),{target:{value:'50'}})
  await waitFor(()=>{
    const last=new URL(calls.filter(url=>url.includes('/decisions')).at(-1)!,'https://lab.test').searchParams
    expect(Object.fromEntries(last)).toEqual({output:'shipped',label:'exclude',labeled:'false',minConfidence:'0.5'})
  })
  expect(screen.getByRole('button',{name:'Filters · 4'})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Clear filters'}))
  await waitFor(()=>expect(new URL(calls.filter(url=>url.includes('/decisions')).at(-1)!,'https://lab.test').search).toBe(''))
})

test('opening a decision shows the item content, revisions, every output, the exact exchange and every label',async()=>{
  render(<LabDecisions/>)
  const list=await screen.findByRole('list',{name:'Decisions'})
  await waitFor(()=>expect(within(list).getAllByRole('listitem')).toHaveLength(2))
  fireEvent.click(within(list).getByText('Story 4'))
  const drawer=await screen.findByRole('dialog',{name:'Item detail'})
  expect(await within(drawer).findByRole('heading',{name:'Story 4'})).toBeVisible()
  expect(within(drawer).getByRole('link',{name:/example\.test\/story-4/})).toHaveAttribute('href','https://example.test/story-4')
  expect(within(drawer).getByText('A short summary.')).toBeVisible()
  const revisions=within(drawer).getByRole('region',{name:'Revision history'})
  expect(within(revisions).getByText(/sent by papyrus-monitor/)).toBeVisible()
  expect(within(revisions).getByText(/source not recorded/)).toBeVisible()
  const outputs=within(drawer).getByRole('region',{name:'Decisions per output'})
  expect(within(outputs).getByText('Request: 12 of 40 examples shown · target trimmed to fit')).toBeVisible()
  const exchange=within(drawer).getByRole('region',{name:'Decision request and response'})
  expect(within(exchange).getByText('One request answered Anthus, Shipped.')).toBeVisible()
  expect(within(exchange).getAllByText(/Exact request target text/).length).toBeGreaterThan(0)
  expect(within(exchange).getAllByText(/Exact model answer/).length).toBeGreaterThan(0)
  const labels=within(drawer).getByRole('region',{name:'Labels'})
  expect(within(labels).getByText('Given on revision 1')).toBeVisible()
  expect(within(labels).getByText(/editor-17 at shipped/)).toBeVisible()
  expect(within(labels).getByText('Not shipped yet.')).toBeVisible()
})

test('an account without a standing run says so, and a refusal is shown rather than an empty list',async()=>{
  vi.stubGlobal('fetch',vi.fn(async(input:string)=>{
    if(input==='/api/session')return respond({authenticated:true,selectedAccountId:'account-1'})
    return respond([])
  }))
  render(<LabDecisions/>)
  expect(await screen.findByText('This account has no standing run.')).toBeVisible()
  cleanup()
  vi.stubGlobal('fetch',vi.fn(async()=>respond({authenticated:false})))
  render(<LabDecisions/>)
  expect(await screen.findByRole('alert')).toHaveTextContent('Sign in to the lab to see decisions.')
})

test('in the hosted lab the Decisions view reads the account selected in the header, not a session lookup',async()=>{
  const lab:{url:string;account:string|undefined}[]=[],graphqlUrls:string[]=[]
  vi.stubGlobal('fetch',vi.fn(async(input:string,init?:RequestInit)=>{
    const url=new URL(input,'https://lab.test')
    if(url.pathname==='/api/session')throw new Error('the hosted Decisions view must not look up the session account')
    if(url.pathname==='/graphql'){
      graphqlUrls.push(input)
      const query=String(JSON.parse(String(init?.body)).query)
      return respond({data:query.includes('capabilities')?{runs:[],capabilities:{liveEnabled:false,itemCount:0}}:{cyclotronDefinitions:[],classifiers:[],itemLists:[]}})
    }
    lab.push({url:url.pathname,account:(init?.headers as Record<string,string>|undefined)?.['X-Cyclotron-Account']})
    if(url.pathname==='/api/lab/standing-runs')return respond([RUN])
    if(url.pathname==='/api/lab/runs/run-1/health')return respond(HEALTH)
    if(url.pathname==='/api/lab/runs/run-1/decisions')return respond(pages[''])
    return respond({detail:'unexpected'},404)
  }))
  render(<WorkspaceTransportProvider accountId="beta"><WebApp embedded/></WorkspaceTransportProvider>)
  await screen.findByText('A history you can inspect')
  const navigation=screen.getByRole('navigation',{name:'Main navigation'})
  fireEvent.click(within(navigation).getByRole('button',{name:'Decisions'}))
  const list=await screen.findByRole('list',{name:'Decisions'})
  await waitFor(()=>expect(within(list).getAllByRole('listitem')).toHaveLength(2))
  expect(lab.map(request=>request.url)).toEqual(expect.arrayContaining(['/api/lab/standing-runs','/api/lab/runs/run-1/decisions']))
  expect(lab.every(request=>request.account==='beta')).toBe(true)
  expect(graphqlUrls.length).toBeGreaterThan(0)
  expect(graphqlUrls.every(url=>new URL(url,'https://lab.test').searchParams.get('account')==='beta')).toBe(true)
})
