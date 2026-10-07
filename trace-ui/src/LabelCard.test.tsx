import {afterEach, expect, test, vi} from 'vitest'
import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {LabelCard} from './LabelCard'

afterEach(cleanup)

test('Scenario: A labeler can compare a displayed prediction with a human label',()=>{
  const submit=vi.fn()
  render(<LabelCard current={{item:{id:'paper',title:'Knowledge access',abstract:'A useful research abstract',submitted_at:'2026-10-06',categories:['cs.AI'],authors:'A. Author',journal_ref:'Journal 2'},prediction:{label:'exclude',confidence:.8,presentation_id:'shown'}}} busy={false} onSubmit={submit} />)
  expect(screen.getByText('A. Author')).toBeVisible()
  expect(screen.getByText('Journal 2')).toBeVisible()
  expect(screen.getByText(/Exclude · 80/)).toBeVisible()
  fireEvent.change(screen.getByLabelText('Explanation (optional)'),{target:{value:'It improves knowledge access'}})
  fireEvent.click(screen.getByRole('button',{name:'Include'}))
  expect(submit).toHaveBeenCalledWith('label',{item_id:'paper',label:'include',comment:'It improves knowledge access',presentation_id:'shown'})
})

test('busy cycles disable voting without losing the explanation',()=>{
  render(<LabelCard current={{item:{id:'p',title:'Paper',abstract:'Abstract',submitted_at:'Today',categories:['AI'],authors:'Author'},prediction:{label:'include',confidence:.7,presentation_id:'shown'}}} busy onSubmit={vi.fn()} />)
  expect(screen.getByRole('button',{name:'Include'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Exclude'})).toBeDisabled()
})
