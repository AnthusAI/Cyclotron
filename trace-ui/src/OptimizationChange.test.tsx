import {cleanup,fireEvent,render,screen,within} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {OptimizationChange} from './OptimizationChange'
import type {TraceEvent} from './graphql'
afterEach(cleanup)
const event=(sequence:number,payload:Record<string,unknown>):TraceEvent=>({sequence,sourceId:String(sequence),payload})
const scope={classifier_id:'a',cycle_id:'c',step_id:'s',step_stage:'rubric'}
const task={name:'include',instructions:'Should this item be included?',labels:['include','exclude']}
const before={config:{task,rubric:'',example_ids:[],tasks:[],dynamic_elements:[]},head:null}
const after={config:{task,rubric:'Include knowledge-base research.',example_ids:['paper'],tasks:[{name:'knowledge',instructions:'Does it discuss knowledge management?',labels:['yes','no']}],dynamic_elements:['current_datetime']},head:{feature_names:['decision/include','knowledge/yes'],calibration:{method:'temperature',temperature:1.1}}}

test('a completed optimization shows recorded rubric questions examples and fitted features before and after',()=>{
  const start=event(1,{...scope,kind:'step-started',classifier_snapshot:before})
  const request=event(2,{...scope,kind:'optimizer-request',messages:[{role:'user',content:JSON.stringify({feedback:[{id:'paper',label:'include',values:{title:'Knowledge graph study',abstract:'A useful abstract'},comment:'Knowledge bases matter'}]})}]})
  const end=event(4,{...scope,kind:'step-completed',status:'completed',classifier_snapshot:after,result:{activated:true,reason:'new trusted evidence'}})
  render(<OptimizationChange event={end} events={[start,request,end]}/>)
  const initial=within(screen.getByRole('region',{name:'Before optimization'}))
  const final=within(screen.getByRole('region',{name:'After optimization'}))
  expect(initial.getByText('Empty rubric')).toBeVisible()
  expect(initial.getByText('Raw decision-model output')).toBeVisible()
  expect(final.getByText('Include knowledge-base research.')).toBeVisible()
  expect(final.getByText('Does it discuss knowledge management?')).toBeVisible()
  expect(final.getByText('Knowledge graph study')).toBeVisible()
  fireEvent.click(final.getByText('Knowledge graph study'))
  expect(final.getByText('Label: include')).toBeVisible()
  expect(final.getByText('knowledge/yes')).toBeVisible()
  expect(screen.getByText('Accepted')).toBeVisible()
})

test('a missing matching snapshot is unavailable rather than borrowed from another classifier or step',()=>{
  const selected=event(3,{...scope,kind:'optimizer-response',content:'proposal'})
  const other=event(1,{...scope,classifier_id:'b',kind:'step-started',classifier_snapshot:before})
  const later=event(5,{...scope,step_id:'another',kind:'step-completed',classifier_snapshot:after})
  render(<OptimizationChange event={selected} events={[other,selected,later]}/>)
  expect(screen.getByText('Before snapshot not recorded for this step')).toBeVisible()
  expect(screen.getByText('After snapshot not recorded for this step')).toBeVisible()
  expect(screen.queryByText('Include knowledge-base research.')).toBeNull()
})

test('the proposed rubric is not presented as an applied rubric when a step rejects it',()=>{
  const start=event(1,{...scope,kind:'step-started',classifier_snapshot:before})
  const selected=event(2,{...scope,kind:'optimizer-response',content:JSON.stringify({rubric:'Not accepted rubric'})})
  const end=event(3,{...scope,kind:'step-completed',status:'completed',classifier_snapshot:before,result:{promoted:false,reason:'no improvement'}})
  render(<OptimizationChange event={selected} events={[start,selected,end]}/>)
  expect(screen.getByText('No change accepted')).toBeVisible()
  expect(screen.queryByText('Not accepted rubric')).toBeNull()
  expect(screen.getAllByText('Empty rubric')).toHaveLength(2)
})

test('unrecorded example text and partial snapshot fields are not guessed',()=>{
  const selected=event(3,{...scope,kind:'step-completed',status:'completed',classifier_snapshot:{config:{rubric:'Recorded',example_ids:['unknown']}}})
  render(<OptimizationChange event={selected} events={[selected]}/>)
  fireEvent.click(screen.getByText('unknown'))
  expect(screen.getByText('Example content not recorded in this step')).toBeVisible()
  expect(screen.getByText('ML head not recorded')).toBeVisible()
  expect(screen.queryByText('Raw decision-model output')).toBeNull()
})
