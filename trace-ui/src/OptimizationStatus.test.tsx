import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {OptimizationStatus,latestOptimizationActivity} from './OptimizationStatus'
import type {TraceEvent} from './graphql'

afterEach(cleanup)
const event=(sequence:number,payload:Record<string,unknown>):TraceEvent=>({sequence,sourceId:String(sequence),payload})
const classifiers=[{id:'a',name:'Relevance',config:{}},{id:'b',name:'Practicality',config:{}}]

test('optimizer response is not presented as a completed or accepted optimization',()=>{
  const events=[event(1,{kind:'optimization-stage-started',stage:'rubric',classifier_id:'a'}),
    event(2,{kind:'optimizer-request',step_stage:'rubric',classifier_id:'a'}),
    event(3,{kind:'optimizer-response',step_stage:'rubric',classifier_id:'a'}),
    event(4,{kind:'human-feedback',classifier_id:'b'})]
  render(<OptimizationStatus events={events} classifiers={classifiers} jobs={[{id:'j',kind:'feedback',status:'running',result:null}]} onInspect={vi.fn()}/>)
  expect(screen.getByText('Relevance · Rubric')).toBeVisible()
  expect(screen.getByText('Response received · validation pending')).toBeVisible()
  expect(screen.queryByText('Accepted')).toBeNull()
})

test('the latest recorded stage completion distinguishes acceptance from no change',()=>{
  const accepted=event(5,{kind:'optimization-stage-completed',stage:'examples',classifier_id:'b',activated:true,promoted:false})
  expect(latestOptimizationActivity([accepted])?.status).toBe('Accepted')
  const unchanged=event(6,{kind:'optimization-stage-completed',stage:'rubric',classifier_id:'a',promoted:false,reason:'waiting for eligible human feedback'})
  const inspect=vi.fn()
  render(<OptimizationStatus events={[unchanged,accepted,unchanged]} classifiers={classifiers} jobs={[]} onInspect={inspect}/>)
  expect(screen.getByText('Completed · no change accepted')).toBeVisible()
  expect(screen.getByText('waiting for eligible human feedback')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Inspect latest optimization event'}))
  expect(inspect).toHaveBeenCalledWith(unchanged)
})

test('a failed command does not leave an earlier optimizer request looking active',()=>{
  render(<OptimizationStatus events={[event(1,{kind:'optimizer-request',step_stage:'rubric',classifier_id:'a'})]}
    classifiers={classifiers} jobs={[{id:'j',kind:'feedback',status:'failed',result:null}]} onInspect={vi.fn()}/>)
  expect(screen.getByText('Command failed · inspect activity before retrying')).toBeVisible()
  expect(screen.queryByText('Optimizer request sent')).toBeNull()
})

test('ML fitting and stage failure use the recorded evidence without inventing optimizer work',()=>{
  const fitting=event(7,{kind:'fit-started',step_stage:'classifier',classifier_id:'b'})
  expect(latestOptimizationActivity([fitting])?.status).toBe('ML fitting started')
  const completed=event(8,{kind:'fit-completed',step_stage:'classifier',classifier_id:'b'})
  expect(latestOptimizationActivity([fitting,completed])?.status).toBe('ML fit completed · selection pending')
  const failed=event(9,{kind:'optimization-stage-failed',stage:'questions',classifier_id:'a',reason:'provider refused request'})
  expect(latestOptimizationActivity([completed,failed])?.status).toBe('Optimization failed')
  render(<OptimizationStatus events={[event(10,{kind:'human-feedback'})]} classifiers={classifiers} jobs={[]} onInspect={vi.fn()}/>)
  expect(screen.getByText('No optimization recorded yet')).toBeVisible()
})

test('native trigger and step events disclose waiting partial failure and budget pauses',()=>{
  expect(latestOptimizationActivity([event(1,{kind:'trigger-evaluated',due:true})])?.status).toBe('Trigger fired · stage queued')
  expect(latestOptimizationActivity([event(1,{kind:'trigger-evaluated',due:false})])?.status).toBe('Trigger checked · not due')
  expect(latestOptimizationActivity([event(2,{kind:'step-completed',status:'waiting',result:{promoted:false}})])?.status).toBe('Waiting for eligible evidence')
  expect(latestOptimizationActivity([event(2,{kind:'step-completed',status:'partial',result:{promoted:false}})])?.status).toBe('Completed with failed trials · inspect activity')
  expect(latestOptimizationActivity([event(2,{kind:'step-completed',status:'failed',result:{}})])?.failed).toBe(true)
  const pause=event(3,{kind:'step-paused',step_stage:'examples',reason:'request budget exhausted'})
  render(<OptimizationStatus events={[pause]} classifiers={classifiers} jobs={[]} onInspect={vi.fn()}/>)
  expect(screen.getByText('Optimization paused · explicit resume required')).toBeVisible()
  expect(screen.getByText('request budget exhausted')).toBeVisible()
})

test('routine no-op trigger checks do not hide the latest completed optimization',()=>{
  const accepted=event(10,{kind:'step-completed',step_stage:'rubric',status:'completed',result:{activated:true}})
  expect(latestOptimizationActivity([accepted,event(11,{kind:'trigger-evaluated',stage:'classifier',due:false})])?.event).toBe(accepted)
})
