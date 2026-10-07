import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardSettings} from './ScorecardSettings'
afterEach(cleanup)

test('changing decision provider clears the other provider model in one settings update',()=>{
  const change=vi.fn()
  render(<ScorecardSettings settings={{decisions_provider:'jev',decisions_model:'jev-pinned',seed:'stable'}} onChange={change} disabled={false}/>)
  fireEvent.change(screen.getByLabelText('Decision provider'),{target:{value:'kev'}})
  expect(change).toHaveBeenCalledExactlyOnceWith({decisions_provider:'kev',seed:'stable'})
})

test('provider selection is disabled while saving and explains unavailable Laya optimization',()=>{
  render(<ScorecardSettings settings={{}} onChange={()=>{}} disabled/>)
  expect(screen.getByLabelText('Decision provider')).toBeDisabled()
  expect(screen.getByText(/Laya.*not yet supported/)).toBeVisible()
})
