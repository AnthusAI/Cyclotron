import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {PromoteSupportingQuestion} from './PromoteSupportingQuestion'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

const checkpoint={cyclotronCheckpoints:[
  {payload:{classifiers:{relevant:{state:{config:{tasks:[{name:'old_question',instructions:'Old?',labels:['yes','no']}]}}}}}},
  {payload:{classifiers:{
    relevant:{state:{config:{tasks:[{name:'names_vendor',instructions:'Does it name a vendor?',labels:['yes','no']}]}}},
    timely:{state:{config:{tasks:[]}}}}}},
]}

function mockApi(){
  vi.mocked(graphql).mockImplementation(async query=>query.includes('promoteSupportingQuestion')
    ?{promoteSupportingQuestion:{id:'new-run'}}:checkpoint)
}

async function fill(){
  fireEvent.click(await screen.findByText('Make a supporting question a classifier'))
  fireEvent.change(await screen.findByRole('combobox',{name:'Supporting question'}),{target:{value:'relevant\u0000names_vendor'}})
  fireEvent.change(screen.getByLabelText('New classifier id'),{target:{value:'vendor'}})
  fireEvent.change(screen.getByLabelText('New classifier name'),{target:{value:'Names a vendor'}})
  fireEvent.change(screen.getByRole('combobox',{name:'Positive label'}),{target:{value:'yes'}})
}

test('it offers the latest checkpoint supporting questions and creates nothing before confirmation',async()=>{
  mockApi()
  render(<PromoteSupportingQuestion runId="run" cyclotronName="Newsroom" nextRevision={3} onCreated={()=>{}} busy={false}/>)
  await fill()
  const options=screen.getAllByRole('option').map(option=>option.textContent)
  expect(options).toContain('relevant · names_vendor: Does it name a vendor?')
  expect(options).not.toContain('relevant · old_question: Old?')
  fireEvent.click(screen.getByRole('button',{name:'Review'}))
  expect(await screen.findByRole('status')).toHaveTextContent('creates version 3 of Newsroom')
  expect(screen.getByRole('status')).toHaveTextContent('starts unlabeled')
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
  fireEvent.click(screen.getByRole('button',{name:'Cancel'}))
  expect(screen.queryByRole('button',{name:'Create version'})).toBeNull()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

test('confirming sends the chosen question and the new classifier with explicit confirmation',async()=>{
  mockApi()
  const created=vi.fn()
  render(<PromoteSupportingQuestion runId="run" cyclotronName="Newsroom" nextRevision={3} onCreated={created} busy={false}/>)
  await fill()
  fireEvent.click(screen.getByRole('button',{name:'Review'}))
  fireEvent.click(await screen.findByRole('button',{name:'Create version'}))
  await waitFor(()=>expect(created).toHaveBeenCalledWith('new-run'))
  const [query,variables]=vi.mocked(graphql).mock.calls.at(-1)!
  expect(query).toContain('promoteSupportingQuestion')
  expect(variables).toEqual({parentRunId:'run',sourceClassifierId:'relevant',question:'names_vendor',
    classifierId:'vendor',classifierName:'Names a vendor',name:'Newsroom',positiveLabel:'yes',confirmed:true})
})

test('review stays disabled until a question, id and name are given, and a server refusal is shown',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>{
    if(query.includes('promoteSupportingQuestion'))throw new Error('cyclotron replay requires explicit live authority')
    return checkpoint
  })
  render(<PromoteSupportingQuestion runId="run" cyclotronName="Newsroom" nextRevision={3} onCreated={()=>{}} busy={false}/>)
  fireEvent.click(await screen.findByText('Make a supporting question a classifier'))
  expect(await screen.findByRole('button',{name:'Review'})).toBeDisabled()
  await fill()
  fireEvent.change(screen.getByRole('combobox',{name:'Positive label'}),{target:{value:''}})
  fireEvent.click(screen.getByRole('button',{name:'Review'}))
  fireEvent.click(await screen.findByRole('button',{name:'Create version'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('explicit live authority')
  expect(vi.mocked(graphql).mock.calls.at(-1)?.[1]).toMatchObject({positiveLabel:null})
})

test('without a checkpointed supporting question it says so instead of offering a form',async()=>{
  vi.mocked(graphql).mockResolvedValue({cyclotronCheckpoints:[]})
  render(<PromoteSupportingQuestion runId="run" cyclotronName="Newsroom" nextRevision={2} onCreated={()=>{}} busy={false}/>)
  fireEvent.click(await screen.findByText('Make a supporting question a classifier'))
  expect(await screen.findByText('No supporting questions are recorded for this version yet.')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Review'})).toBeNull()
})
