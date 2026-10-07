import {render,screen,cleanup} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ExchangeDetail,exchangeEvents} from './ExchangeDetail'
import type {TraceEvent} from './graphql'
afterEach(cleanup)
const event=(sequence:number,payload:Record<string,unknown>):TraceEvent=>({sequence,sourceId:String(sequence),payload})

test('selecting an optimizer response shows its full request and tool calls without another classifiers context',()=>{
  const scope={classifier_id:'a',step_id:'s',briefing_fingerprint:'f'}
  const request=event(1,{...scope,kind:'optimizer-request',messages:[{role:'user',content:'Human explanation: knowledge bases matter'}]})
  const response=event(3,{...scope,kind:'optimizer-response',content:'New rubric',tool_calls:[{name:'propose'}]})
  const unrelated=event(2,{...scope,classifier_id:'b',kind:'optimizer-request',messages:[{role:'user',content:'Unrelated feedback'}]})
  render(<ExchangeDetail event={response} events={[request,unrelated,response]}/> )
  expect(screen.getByText('Human explanation: knowledge bases matter')).toBeVisible()
  expect(screen.getByText('New rubric')).toBeVisible()
  expect(screen.queryByText('Unrelated feedback')).toBeNull()
  expect(screen.getByText('Tool calls')).toBeInTheDocument()
})

test('decision inspection includes the request and response for that target and step only',()=>{
  const scope={classifier_id:'a',step_id:'p',cycle_id:'c',target_id:'item'}
  const events=[event(1,{...scope,kind:'decision-request',state:{rubric:'actual'}}),event(2,{...scope,target_id:'other',kind:'decision-response'}),event(3,{...scope,kind:'decision-response',answers:{decision:'yes'}}),event(4,{...scope,kind:'prediction'})]
  expect(exchangeEvents(events[3],events).map(row=>row.sequence)).toEqual([1,3])
})

test('missing correlation does not substitute an unrelated nearby model call',()=>{
  expect(exchangeEvents(event(2,{kind:'proposal-validated'}),[event(1,{kind:'optimizer-request'})])).toEqual([])
})

test('inspecting a completed rubric stage includes its exact optimizer request and response',()=>{
  const scope={classifier_id:'a',step_id:'rubric-1',step_stage:'rubric'}
  const request=event(1,{...scope,kind:'optimizer-request',messages:[{role:'user',content:'Preserve the explanation about knowledge bases'}]})
  const response=event(2,{...scope,kind:'optimizer-response',content:'A refined rubric'})
  const completion=event(4,{...scope,kind:'step-completed',result:{activated:true}})
  render(<ExchangeDetail event={completion} events={[request,response,event(3,{...scope,step_id:'another-step',kind:'optimizer-request'}),completion]}/> )
  expect(screen.getByText('Preserve the explanation about knowledge bases')).toBeVisible()
  expect(screen.getByText('A refined rubric')).toBeVisible()
})
