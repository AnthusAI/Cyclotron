import {cleanup,fireEvent,render,screen,within} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {readFileSync} from 'node:fs'
import example from '../../../src/decision_flywheel/schemas/cyclotron-status.v1.example.json'
import type {CyclotronStatus} from '../cyclotronStatus'
import {CyclotronStatusView} from './cyclotron-status'

afterEach(cleanup)
const status=example as CyclotronStatus

it('renders the strip from a snapshot in words',()=>{
  render(<CyclotronStatusView status={status} />)
  const strip=screen.getByRole('region',{name:'papyrus-relevance status'})
  expect(strip).toHaveTextContent('Version 13')
  expect(strip).toHaveTextContent('Agrees 86%')
  expect(strip).toHaveTextContent('Says 84%, right 86%')
  expect(strip).toHaveTextContent('▼ Tapering: reviewing 25% of confident decisions')
  expect(strip).toHaveTextContent('7 awaiting review')
})

it('explains each alignment number under its name in the card',()=>{
  render(<CyclotronStatusView status={status} variant="card" />)
  const recall=screen.getByTestId('metric-recall')
  expect(within(recall).getByText('90%')).toBeVisible()
  expect(within(recall).getByText('Of the items that should be a yes (include), how many it found.')).toBeVisible()
  expect(within(screen.getByTestId('metric-precision')).getByText('Of the yeses it gave (include), how many were right.')).toBeVisible()
  expect(within(screen.getByTestId('metric-accuracy')).getByText('Of all items, how many it got right.')).toBeVisible()
  expect(screen.getByText('Says 84%, right 86% · off by 2 points')).toBeVisible()
  expect(screen.getByText(status.reviewRate.reason)).toBeVisible()
  expect(screen.getByText(`Next: ${status.reviewRate.nextStep}`)).toBeVisible()
  expect(screen.getByText(/Version 12 → 13: Added question: is this vendor marketing\?/)).toBeVisible()
})

it('says what is not measured yet instead of showing empty numbers',()=>{
  const empty:CyclotronStatus={...status,cyclotron:{...status.cyclotron,version:1,refits:0},
    alignment:{...status.alignment,labels:0,accuracy:null,precision:null,recall:null},
    calibration:{saysSure:null,isRight:null,gapPoints:null,curve:[]},
    reviewRate:{...status.reviewRate,state:'onboarding',rate:1},lastChange:null,
    pending:{decisionsAwaitingReview:0,staleSince:null}}
  render(<CyclotronStatusView status={empty} variant="card" />)
  expect(screen.getAllByText('Not measured yet')).toHaveLength(3)
  expect(screen.getByText('Calibration not measured yet')).toBeVisible()
  expect(screen.getByRole('heading',{name:'Onboarding: reviewing every decision'})).toHaveTextContent('○ Onboarding')
  expect(screen.queryByRole('region',{name:'Last change'})).toBeNull()
})

it('reports a refit as minor and keeps the version',()=>{
  const refit:CyclotronStatus={...status,cyclotron:{...status.cyclotron,refits:2},
    lastChange:{kind:'refit',fromVersion:13,toVersion:13,at:null,summary:'Refit the ML model on 140 labels.',labels:140}}
  render(<CyclotronStatusView status={refit} variant="card" />)
  expect(screen.getByText('Version 13 (2 refits) · as of',{exact:false})).toBeVisible()
  expect(screen.getByText('Refit on 140 labels, still version 13 (minor)')).toBeVisible()
})

it('shows an override with who set it and when it ends, and offers a change',()=>{
  const onOverride=vi.fn()
  const manual:CyclotronStatus={...status,reviewRate:{...status.reviewRate,state:'manual',rate:.5,
    reason:'Manual rate of 50% set by managing-editor.',
    override:{rate:.5,expiresAt:'2026-10-20T00:00:00+00:00',setBy:'managing-editor',setAt:'2026-10-09T00:00:00+00:00'}}}
  render(<CyclotronStatusView status={manual} variant="card" onOverride={onOverride} />)
  expect(screen.getByRole('heading',{name:'Manual rate: reviewing 50% of confident decisions'})).toHaveTextContent('✎ Manual rate')
  expect(screen.getByText(/Manual rate 50% set by managing-editor until/)).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Change review rate'}))
  expect(onOverride).toHaveBeenCalled()
})

it('gives every review state a mark and a word',()=>{
  for(const [state,word] of [['full','Full review'],['raised','Raised to full review'],['steady','Steady']] as const){
    render(<CyclotronStatusView status={{...status,reviewRate:{...status.reviewRate,state}}} />)
    expect(screen.getByRole('region').querySelector('[data-review-state]')).toHaveTextContent(word)
    cleanup()
  }
})

it('ships its styles in components.css',()=>{
  const css=readFileSync('src/styles/components.css','utf8')
  expect(css).toContain('.cyclotron-status[data-variant="strip"]')
  expect(css).toContain('.cyclotron-status-metric')
})
