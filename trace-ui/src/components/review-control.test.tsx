import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {readFileSync} from 'node:fs'
import {ReviewControl,type ReviewReason} from './review-control'

afterEach(()=>{cleanup();vi.restoreAllMocks()})

const decision={decisionId:'d-1',label:'include',confidence:.82,classes:['include','exclude'],version:3}
const reasons:ReviewReason[]=[
  {code:'out_of_scope',text:'Out of scope'},
  {code:'duplicate',text:'Duplicate',noLabel:true},
]

it('shows the decision, its confidence and why it was sent to review',()=>{
  render(<ReviewControl itemId="ref-1" decision={decision} question="Is this relevant to the publication?" reviewReason="Random audit sample." />)
  expect(screen.getByRole('heading',{name:'Is this relevant to the publication?'})).toBeVisible()
  expect(screen.getByText(/82% sure · version 3/)).toBeVisible()
  expect(screen.getByText('Why you are seeing this: Random audit sample.')).toBeVisible()
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
})

it('reports a thumbs-up review with an explanation and makes no network call',()=>{
  const fetch=vi.spyOn(globalThis,'fetch').mockImplementation(()=>{throw new Error('no network')})
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('button',{name:'Yes: include'}))
  expect(screen.getByRole('button',{name:'Yes: include'})).toHaveAttribute('aria-pressed','true')
  expect(screen.queryByLabelText(/Reason/)).toBeNull()
  fireEvent.change(screen.getByLabelText('Explanation (optional)'),{target:{value:'  On our beat.  '}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'include',reasonCode:null,explanation:'On our beat.'})
  expect(fetch).not.toHaveBeenCalled()
})

it('needs a reason for thumbs down and marks the correction in words, not colour alone',()=>{
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('button',{name:'No: exclude'}))
  expect(screen.getByText('This is a correction: the decision was include.')).toBeVisible()
  expect(screen.getByRole('button',{name:'No: exclude'})).toHaveTextContent('✗')
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Reason'),{target:{value:'out_of_scope'}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'exclude',reasonCode:'out_of_scope',explanation:null})
})

it('a no-label reason submits a review without a label',()=>{
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('button',{name:'No: exclude'}))
  fireEvent.change(screen.getByLabelText('Reason'),{target:{value:'duplicate'}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith(expect.objectContaining({label:null,reasonCode:'duplicate'}))
})

it('maps thumbs to a chosen positive class and offers one button per class in labels mode',()=>{
  const onReview=vi.fn()
  const {rerender}=render(<ReviewControl itemId="ref-1" decision={decision} positiveLabel="exclude" onReview={onReview} />)
  expect(screen.getByRole('button',{name:'Yes: exclude'})).toBeVisible()
  const three={decisionId:'d-2',label:'later',confidence:null,classes:['now','later','never']}
  rerender(<ReviewControl itemId="ref-2" decision={three} onReview={onReview} />)
  expect(screen.getByText(/confidence unknown/)).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'never'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith(expect.objectContaining({itemId:'ref-2',decisionId:'d-2',label:'never'}))
})

it('clears its draft when the decision changes',()=>{
  const {rerender}=render(<ReviewControl itemId="ref-1" decision={decision} />)
  fireEvent.click(screen.getByRole('button',{name:'Yes: include'}))
  rerender(<ReviewControl itemId="ref-3" decision={{...decision,decisionId:'d-3'}} />)
  expect(screen.getByRole('button',{name:'Yes: include'})).toHaveAttribute('aria-pressed','false')
})

it('shows a recorded review read-only with undo',()=>{
  const onUndo=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} value={{label:'exclude',reasonCode:'out_of_scope',explanation:'Vendor marketing.'}} onUndo={onUndo} />)
  expect(screen.getByText(/Correction: exclude · Out of scope/)).toBeVisible()
  expect(screen.getByText('“Vendor marketing.”')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Submit review'})).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Undo review'}))
  expect(onUndo).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1'})
})

it('ships its styles, and the labeling review styles, in components.css',()=>{
  const css=readFileSync('src/styles/components.css','utf8')
  for(const selector of ['.cyclotron-review-choice','.cyclotron-review-submit','.label-choice[data-agreement="correct"]','.prediction-confidence','.confidence-key'])
    expect(css).toContain(selector)
  expect(readFileSync('src/index.css','utf8')).not.toContain('.prediction-confidence {')
})
