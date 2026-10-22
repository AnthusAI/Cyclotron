import {cleanup,fireEvent,render,screen,within} from '@testing-library/react'
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

it('requires an explicit explanation when the host requests one',()=>{
 const onReview=vi.fn()
 render(<ReviewControl itemId="reference-publication" decision={decision} requireExplanation onReview={onReview}/>)
 fireEvent.click(screen.getByRole('radio',{name:'Yes: include'}))
 expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
 expect(screen.getByLabelText(/^Why\? A sentence or two helps it learn.*\(required\)/)).toBeRequired()
 fireEvent.change(screen.getByLabelText(/^Why\? A sentence or two helps it learn.*\(required\)/),{target:{value:'  '}})
 expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
 fireEvent.change(screen.getByLabelText(/^Why\? A sentence or two helps it learn.*\(required\)/),{target:{value:'Original evidence for this publication.'}})
 fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
 expect(onReview).toHaveBeenCalledWith(expect.objectContaining({explanation:'Original evidence for this publication.'}))
})

it('shows the decision, its confidence and why it was sent to review',()=>{
  render(<ReviewControl itemId="ref-1" decision={decision} question="Is this relevant to the publication?" reviewReason="Random audit sample." />)
  expect(screen.getByRole('heading',{name:'Is this relevant to the publication?'})).toBeVisible()
  expect(screen.getByText(/Suggested:/)).toHaveTextContent('Suggested: include (82% sure)')
  expect(screen.getByText(/Suggested:/)).not.toHaveTextContent(/version/i)
  expect(screen.getByText('Why you are seeing this: Random audit sample.')).toBeVisible()
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
})

it('reports a thumbs-up review with an explanation and makes no network call',()=>{
  const fetch=vi.spyOn(globalThis,'fetch').mockImplementation(()=>{throw new Error('no network')})
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('radio',{name:'Yes: include'}))
  expect(screen.getByRole('radio',{name:'Yes: include'})).toHaveAttribute('aria-checked','true')
  expect(screen.queryByLabelText(/Reason/)).toBeNull()
  fireEvent.change(screen.getByLabelText(/^Why\? A sentence or two helps it learn/),{target:{value:'  On our beat.  '}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'include',reasonCode:null,explanation:'On our beat.'})
  expect(fetch).not.toHaveBeenCalled()
})

it('needs a reason for thumbs down and marks the correction in words, not colour alone',()=>{
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  expect(screen.getByText('This is a correction: the decision was include.')).toBeVisible()
  expect(screen.getByRole('radio',{name:'No: exclude'})).toHaveTextContent('✗')
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
  fireEvent.click(screen.getByRole('radio',{name:'Out of scope'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'exclude',reasonCode:'out_of_scope',explanation:null})
})

it('a no-label reason submits a review without a label',()=>{
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  fireEvent.click(screen.getByRole('radio',{name:/^Duplicate/}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith(expect.objectContaining({label:null,reasonCode:'duplicate'}))
})

it('maps thumbs to a chosen positive class and offers one button per class in labels mode',()=>{
  const onReview=vi.fn()
  const {rerender}=render(<ReviewControl itemId="ref-1" decision={decision} positiveLabel="exclude" onReview={onReview} />)
  expect(screen.getByRole('radio',{name:'Yes: exclude'})).toBeVisible()
  const three={decisionId:'d-2',label:'later',confidence:null,classes:['now','later','never']}
  rerender(<ReviewControl itemId="ref-2" decision={three} onReview={onReview} />)
  expect(screen.getByText(/Suggested:/)).toHaveTextContent(/^Suggested: later$/)
  fireEvent.click(screen.getByRole('radio',{name:'never'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith(expect.objectContaining({itemId:'ref-2',decisionId:'d-2',label:'never'}))
})

it('falls back to one button per class when thumbs mode is requested with more than two classes',()=>{
  const three={decisionId:'d-2',label:'later',confidence:null,classes:['now','later','never']}
  render(<ReviewControl itemId="ref-2" decision={three} mode="thumbs" />)
  expect(screen.getByRole('radio',{name:'never'})).toBeVisible()
})

it('clears its draft when the decision changes',()=>{
  const {rerender}=render(<ReviewControl itemId="ref-1" decision={decision} />)
  fireEvent.click(screen.getByRole('radio',{name:'Yes: include'}))
  rerender(<ReviewControl itemId="ref-3" decision={{...decision,decisionId:'d-3'}} />)
  expect(screen.getByRole('radio',{name:'Yes: include'})).toHaveAttribute('aria-checked','false')
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

it('works with no reason list at all',()=>{
  const onReview=vi.fn()
  render(<ReviewControl itemId="ref-1" decision={decision} onReview={onReview} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  expect(screen.queryByLabelText(/Reason/)).toBeNull()
  expect(screen.getByRole('button',{name:'Submit review'})).toBeEnabled()
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'exclude',reasonCode:null,explanation:null})
})

it('derives its field ids from the item and decision, not from render order',()=>{
  render(<ReviewControl itemId="ref 1" decision={decision} reasons={reasons} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  expect(screen.getByLabelText(/^Why\? A sentence or two helps it learn/)).toHaveAttribute('id','cyclotron-review-ref_1-d-1-explanation')
  expect(screen.getByRole('radiogroup',{name:'Reason'})).toHaveAttribute('aria-labelledby','cyclotron-review-ref_1-d-1-reason-label')
})

it('offers the reason as visible radio buttons, never a dropdown, with the explanation below',()=>{
  const onReview=vi.fn()
  const {container}=render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} onReview={onReview} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  expect(container.querySelector('select')).toBeNull()
  expect(screen.queryByRole('combobox')).toBeNull()
  const group=screen.getByRole('radiogroup',{name:'Reason'})
  const radios=within(group).getAllByRole('radio')
  expect(radios.map(r=>r.textContent)).toEqual(['Out of scope','Duplicate (no label)'])
  radios.forEach(r=>{expect(r).toBeVisible();expect(r).toHaveAttribute('aria-checked','false')})
  fireEvent.click(radios[0])
  expect(screen.getByRole('radio',{name:'Out of scope'})).toHaveAttribute('aria-checked','true')
  expect(radios[1]).toHaveAttribute('aria-checked','false')
  const box=screen.getByLabelText(/^Why\? A sentence or two helps it learn what you want\./)
  expect(box.tagName).toBe('TEXTAREA')
  expect(group.compareDocumentPosition(box)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  const choices=screen.getByRole('radiogroup',{name:'Your label'})
  expect(choices.compareDocumentPosition(group)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(box).toHaveAttribute('placeholder',expect.stringContaining('learns from'))
  fireEvent.change(box,{target:{value:'Not our beat.'}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(onReview).toHaveBeenCalledWith({itemId:'ref-1',decisionId:'d-1',label:'exclude',reasonCode:'out_of_scope',explanation:'Not our beat.'})
})

it('moves and selects reasons with the arrow keys using one tab stop',()=>{
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} />)
  fireEvent.click(screen.getByRole('radio',{name:'No: exclude'}))
  const [first,second]=within(screen.getByRole('radiogroup',{name:'Reason'})).getAllByRole('radio')
  expect(first).toHaveAttribute('tabindex','0')
  expect(second).toHaveAttribute('tabindex','-1')
  first.focus()
  fireEvent.keyDown(first,{key:'ArrowRight'})
  expect(second).toHaveAttribute('aria-checked','true')
  expect(second).toHaveFocus()
  expect(second).toHaveAttribute('tabindex','0')
  fireEvent.keyDown(second,{key:'ArrowLeft'})
  expect(first).toHaveAttribute('aria-checked','true')
  fireEvent.keyDown(first,{key:'ArrowLeft'})
  expect(second).toHaveAttribute('aria-checked','true')
})

it('lets an optional reason be cleared by choosing it again',()=>{
  render(<ReviewControl itemId="ref-1" decision={decision} reasons={reasons} mode="labels" />)
  fireEvent.click(screen.getByRole('radio',{name:'exclude'}))
  const radio=screen.getByRole('radio',{name:'Out of scope'})
  fireEvent.click(radio)
  expect(radio).toHaveAttribute('aria-checked','true')
  fireEvent.click(radio)
  expect(radio).toHaveAttribute('aria-checked','false')
})
