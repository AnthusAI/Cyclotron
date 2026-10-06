import {afterEach, expect, test} from 'vitest'
import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {ExchangePanel} from './ExchangePanel'

afterEach(cleanup)

test('request and response tabs expose recorded content without losing it when switching',()=>{
  render(<ExchangePanel kind="decision" title="Decision model" />)
  document.getElementById('decision-exchange')!.hidden=false
  document.getElementById('decision-request-content')!.textContent='full target and rubric'
  document.getElementById('decision-response-content')!.textContent='returned probabilities'
  expect(screen.getByRole('tab',{name:'Request'})).toHaveAttribute('aria-selected','true')
  expect(screen.getByText('full target and rubric')).toBeVisible()
  expect(screen.getByText('returned probabilities')).not.toBeVisible()
  fireEvent.mouseDown(screen.getByRole('tab',{name:'Response'}),{button:0,ctrlKey:false})
  expect(screen.getByText('returned probabilities')).toBeVisible()
  expect(screen.getByText('full target and rubric')).not.toBeVisible()
  fireEvent.mouseDown(screen.getByRole('tab',{name:'Request'}),{button:0,ctrlKey:false})
  expect(screen.getByText('full target and rubric')).toBeVisible()
})
