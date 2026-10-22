import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import example from '../../../src/decision_flywheel/schemas/cyclotron-status.v1.example.json'
import type {CyclotronStatus} from '../cyclotronStatus'
import type {CyclotronDecision} from '../cyclotronSdk'
import {CyclotronHeader,PendingItem} from './embedding'

afterEach(cleanup)

const decision:CyclotronDecision={decisionId:'d-1',itemId:'ref-1',createdAt:'2026-10-09T10:00:00+00:00',
  classifiers:{relevant:{label:'include',confidence:.82,probabilities:{include:.82,exclude:.18},version:13,fingerprint:'f'}},
  review:{selected:true,reason:'audit',propensity:.25,detail:'Random audit sample.'}}

it('the documented pending item reports a review',()=>{
  const onReview=vi.fn()
  render(<PendingItem itemId="ref-1" decision={decision} onReview={onReview} />)
  expect(screen.getByText('Why you are seeing this: Random audit sample.')).toBeVisible()
  expect(screen.getByText(/Suggested:/)).toHaveTextContent('Suggested: include (82% sure)')
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  fireEvent.click(screen.getByRole('radio',{name:'Out of scope'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'exclude',reasonCode:'out_of_scope',explanation:null})
})

it('the documented header renders the strip and the card',()=>{
  const onOverride=vi.fn()
  render(<CyclotronHeader status={example as CyclotronStatus} onOverride={onOverride} />)
  expect(screen.getAllByRole('region',{name:'papyrus-relevance status'})).toHaveLength(2)
  expect(screen.getByText('How suggestions are doing')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Change review rate'}))
  expect(onOverride).toHaveBeenCalled()
})
