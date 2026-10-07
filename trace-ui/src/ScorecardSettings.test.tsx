import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardSettings,validScorecardSettings} from './ScorecardSettings'
afterEach(cleanup)

test('optimizer transport selection preserves the explicit model and other shared defaults',()=>{
  const change=vi.fn()
  render(<ScorecardSettings settings={{optimizer_model:'anthropic/fake',seed:'stable'}} onChange={change} disabled={false}/>)
  fireEvent.change(screen.getByLabelText('Optimizer transport'),{target:{value:'litellm'}})
  expect(change).toHaveBeenCalledExactlyOnceWith({optimizer_transport:'litellm',optimizer_model:'anthropic/fake',seed:'stable'})
  expect(validScorecardSettings({optimizer_transport:'unknown'})).toBe(false)
  expect(validScorecardSettings({optimizer_transport:'litellm'})).toBe(true)
})

test('changing decision provider clears the other provider model in one settings update',()=>{
  const change=vi.fn()
  render(<ScorecardSettings settings={{decisions_provider:'jev',decisions_model:'jev-pinned',seed:'stable'}} onChange={change} disabled={false}/>)
  fireEvent.change(screen.getByLabelText('Decision provider'),{target:{value:'kev'}})
  expect(change).toHaveBeenCalledExactlyOnceWith({decisions_provider:'kev',seed:'stable'})
})

test('provider selection is disabled while saving and explains Laya context limits',()=>{
  render(<ScorecardSettings settings={{}} onChange={()=>{}} disabled/>)
  expect(screen.getByLabelText('Decision provider')).toBeDisabled()
  expect(screen.getByRole('option',{name:'Laya (local checkpoint)'})).toBeInTheDocument()
  expect(screen.getByText(/Laya.*rejects context/)).toBeVisible()
})

test('selecting Laya preserves shared settings and clears the previous model',()=>{
  const change=vi.fn()
  render(<ScorecardSettings settings={{decisions_provider:'jev',decisions_model:'jev-pinned',seed:'stable'}} onChange={change} disabled={false}/>)
  fireEvent.change(screen.getByLabelText('Decision provider'),{target:{value:'laya'}})
  expect(change).toHaveBeenCalledExactlyOnceWith({decisions_provider:'laya',seed:'stable'})
})
