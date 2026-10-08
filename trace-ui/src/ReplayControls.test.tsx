import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {it,expect,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ReplayControls} from './ReplayControls'
afterEach(cleanup)

it('an inactive version stops replay without pretending to process and requires a fresh start after activation',()=>{
  const advance=vi.fn()
  const {rerender}=render(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  fireEvent.click(screen.getByRole('button',{name:'Run replay'}))
  rerender(<ReplayControls busy finished={false} onAdvance={advance}/>)
  rerender(<ReplayControls busy={false} readOnly finished={false} onAdvance={advance}/>)
  expect(screen.getByRole('button',{name:'Run replay'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Step replay'})).toBeDisabled()
  expect(screen.queryByText(/Processing a replay cycle/)).toBeNull()
  rerender(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  expect(advance).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button',{name:'Run replay'}))
  expect(advance).toHaveBeenCalledTimes(2)
})

it('does not advance another cycle after pausing during a running cycle',()=>{
  const advance=vi.fn()
  const {rerender}=render(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  fireEvent.click(screen.getByRole('button',{name:'Run replay'}))
  expect(advance).toHaveBeenCalledOnce()
  rerender(<ReplayControls busy finished={false} onAdvance={advance}/>)
  fireEvent.click(screen.getByRole('button',{name:'Pause replay'}))
  rerender(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  expect(advance).toHaveBeenCalledOnce()
})

it('requires a fresh explicit run action after a failure is cleared',()=>{
  const advance=vi.fn()
  const {rerender}=render(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  fireEvent.click(screen.getByRole('button',{name:'Run replay'}))
  rerender(<ReplayControls busy finished={false} onAdvance={advance}/>)
  rerender(<ReplayControls busy={false} failed finished={false} onAdvance={advance}/>)
  rerender(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  expect(advance).toHaveBeenCalledOnce()
  expect(screen.getByRole('button',{name:'Run replay'})).toBeVisible()
})

it('steps replay only when explicitly requested and pauses when busy',()=>{
  const advance=vi.fn()
  const {rerender}=render(<ReplayControls busy={false} finished={false} onAdvance={advance}/>)
  expect(advance).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Step replay'}))
  expect(advance).toHaveBeenCalledOnce()
  rerender(<ReplayControls busy finished={false} onAdvance={advance}/>)
  expect(screen.getByRole('button',{name:'Step replay'})).toBeDisabled()
})
