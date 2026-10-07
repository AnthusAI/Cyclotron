import {afterEach,expect,test} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {TraceDetail} from './WebApp'

afterEach(cleanup)

test('decision response answers are visible without opening the raw event',()=>{
  render(<TraceDetail event={{sequence:3,sourceId:'engine:3',payload:{kind:'decision-response',answers:{decision:{value:'include',confidence:.9}}}}} />)
  expect(screen.getByText('Decision answers')).toBeVisible()
  expect(screen.getByText('Decision answers').parentElement?.querySelector('pre')).toHaveTextContent('"confidence": 0.9')
})

test('optimizer request exposes the human explanation in its full context',()=>{
  render(<TraceDetail event={{sequence:4,sourceId:'engine:4',payload:{kind:'optimizer-request',messages:[{role:'user',content:'{"human_explanations":["Knowledge-base management is important"]}'}]}}} />)
  expect(screen.getByText(/full request context/).parentElement?.querySelector('pre')).toHaveTextContent('Knowledge-base management is important')
})
