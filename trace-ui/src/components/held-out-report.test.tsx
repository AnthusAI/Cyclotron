import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import example from '../../../src/decision_flywheel/schemas/cyclotron-held-out-report.v1.example.json'
import {createHeldOutClient,isHeldOutReport,type HeldOutClient,type HeldOutOverview,type HeldOutReport} from '../heldOutReport'
import {HeldOutPanel,HeldOutReportView} from './held-out-report'

afterEach(cleanup)
const report=example as HeldOutReport

it('leads with the headline change and the verdict in words',()=>{
  render(<HeldOutReportView report={report} classifierName="Relevant to the publication" />)
  expect(screen.getByRole('heading',{name:'Relevant to the publication: before and after'})).toBeVisible()
  const headline=screen.getByTestId('cyclotron-held-out-headline')
  expect(headline).toHaveTextContent('Balanced accuracy')
  expect(headline).toHaveTextContent('46% → 87% +41.1 points')
  expect(headline).toHaveTextContent('▲ Clear improvement')
  expect(screen.getByTestId('cyclotron-held-out-pairs')).toHaveTextContent('Newly right on 13 · newly wrong on 0 · sign test p < 0.001')
  expect(screen.getByText(/40 held-out items \(12 include, 28 exclude\)/)).toBeVisible()
})

it('shows both endpoints side by side for every measure',()=>{
  render(<HeldOutReportView report={report} />)
  const table=screen.getByRole('table')
  expect(within(table).getByRole('columnheader',{name:'Before · version 1'})).toBeVisible()
  expect(within(table).getByRole('columnheader',{name:'After · version 4 (2 refits)'})).toBeVisible()
  const recall=within(table).getByRole('row',{name:/Recall \(include\)/})
  expect(recall).toHaveTextContent(/Recall \(include\)\s*24%\s*81%/)
  expect(within(table).getByRole('row',{name:/Agrees on exclude/})).toBeVisible()
})

it('explains weighting, leftovers and cost in plain notes',()=>{
  const adjusted:HeldOutReport={...report,heldOutSet:{...report.heldOutSet,excludedSinceFrozen:1,labelsChangedSinceFrozen:2}}
  render(<HeldOutReportView report={adjusted} />)
  expect(screen.getByText(/each counts with the inverse of its chance of review/)).toBeVisible()
  expect(screen.getByText('1 frozen item later used for training is left out.')).toBeVisible()
  expect(screen.getByText('2 labels corrected since freezing; the frozen labels are still used.')).toBeVisible()
  expect(screen.getByText('This evaluation made 44 new model requests and answered 36 from cache.')).toBeVisible()
})

it('says when there is nothing to report yet and offers to freeze',()=>{
  const onFreeze=vi.fn()
  render(<HeldOutReportView report={null} onFreeze={onFreeze} />)
  expect(screen.getByText(/No held-out set yet/)).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Freeze held-out set'}))
  expect(onFreeze).toHaveBeenCalledOnce()
  expect(screen.queryByRole('button',{name:/Evaluate/})).toBeNull()
})

it('narrows stored JSON to a report',()=>{
  expect(isHeldOutReport(report)).toBe(true)
  expect(isHeldOutReport({schema:'cyclotron-status/v1'})).toBe(false)
})

it('sends the two shared GraphQL operations and evaluates with the confirmed bound',async()=>{
  const set={id:'held-out-1',frozenAt:'2026-10-16T09:00:00Z',items:40,labelCounts:{include:12,exclude:28},weighted:false,selection:'x'}
  let overview:HeldOutOverview={cyclotronId:'papyrus-relevance',classifier:null,sets:[set],latestReport:null,reportHistory:[],requestBound:80,allowLive:true}
  const request=vi.fn(async(query:string,_variables:Record<string,unknown>)=>{
    if(query.includes('heldOutOverview'))return {heldOutOverview:overview}
    overview={...overview,latestReport:report}
    return {heldOutCommand:report}
  })
  const client:HeldOutClient=createHeldOutClient(request as never)
  render(<HeldOutPanel client={client} cyclotronId="papyrus-relevance" />)
  fireEvent.click(await screen.findByRole('button',{name:'Evaluate (up to 80 model requests)'}))
  await waitFor(()=>expect(screen.getByTestId('cyclotron-held-out-headline')).toHaveTextContent('46% → 87%'))
  const evaluation=request.mock.calls.find(([query])=>query.includes('heldOutCommand'))!
  expect(evaluation[1]).toEqual({action:'evaluate',payload:{cyclotronId:'papyrus-relevance',classifier:null,confirmedRequestBound:80}})
})

it('shows a refusal from the host instead of a stale card',async()=>{
  const client:HeldOutClient={overview:vi.fn(async()=>{throw new Error('not this caller’s cyclotron')}),freeze:vi.fn(),evaluate:vi.fn()}
  render(<HeldOutPanel client={client} cyclotronId="another" />)
  expect(await screen.findByText('not this caller’s cyclotron')).toBeVisible()
})
