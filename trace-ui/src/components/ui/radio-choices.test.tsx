import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {RadioChoices} from './radio-choices'

afterEach(cleanup)
const choices=[{value:'a',content:'Alpha'},{value:'b',content:'Beta'},{value:'c',content:'Gamma'}]

it('is a radiogroup of visible buttons with aria-checked and one tab stop',()=>{
  render(<RadioChoices ariaLabel="Pick" choices={choices} value="b" onChange={vi.fn()}/>)
  expect(screen.getByRole('radiogroup',{name:'Pick'})).toBeVisible()
  expect(screen.getByRole('radio',{name:'Beta'})).toHaveAttribute('aria-checked','true')
  expect(screen.getByRole('radio',{name:'Alpha'})).toHaveAttribute('aria-checked','false')
  expect(screen.getAllByRole('radio').map(r=>r.getAttribute('tabindex'))).toEqual(['-1','0','-1'])
})

it('moves with arrow keys, wrapping, and only clears when clearable',()=>{
  const onChange=vi.fn()
  const {rerender}=render(<RadioChoices ariaLabel="Pick" choices={choices} value="c" onChange={onChange}/>)
  fireEvent.keyDown(screen.getByRole('radio',{name:'Gamma'}),{key:'ArrowRight'})
  expect(onChange).toHaveBeenLastCalledWith('a')
  fireEvent.keyDown(screen.getByRole('radio',{name:'Gamma'}),{key:'ArrowUp'})
  expect(onChange).toHaveBeenLastCalledWith('b')
  fireEvent.click(screen.getByRole('radio',{name:'Gamma'}))
  expect(onChange).toHaveBeenLastCalledWith('c')
  rerender(<RadioChoices ariaLabel="Pick" choices={choices} value="c" clearable onChange={onChange}/>)
  fireEvent.click(screen.getByRole('radio',{name:'Gamma'}))
  expect(onChange).toHaveBeenLastCalledWith('')
})
