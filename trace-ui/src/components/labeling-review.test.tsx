import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {createRef} from 'react'
import {LabelingReview} from './labeling-review'

afterEach(cleanup)

const prediction={label:'publish',confidence:.8,classes:['publish','reject'],probabilities:{publish:.8,reject:.2}}

it('is a controlled, non-submitting label and explanation surface with stable replay targets',()=>{
  const onLabel=vi.fn(),onExplanation=vi.fn(),explanationRef=createRef<HTMLTextAreaElement>()
  render(<LabelingReview targetId="cycle-17" classifierName="Editorial decision" prediction={prediction} selectedLabel="reject" explanationDraft="Recorded reason" simulation onSelectedLabelChange={onLabel} onExplanationDraftChange={onExplanation} explanationRef={explanationRef} />)
  const review=screen.getByTestId('labeling-review-cycle-17')
  expect(review).toHaveAttribute('data-cyclotron-simulation','true')
  expect(screen.getByRole('button',{name:'Editorial decision: reject'})).toHaveAttribute('aria-pressed','true')
  expect(screen.getByLabelText('Explanation for Editorial decision')).toHaveAttribute('data-cyclotron-explanation-target','cycle-17')
  expect(explanationRef.current).toBe(screen.getByLabelText('Explanation for Editorial decision'))
  fireEvent.click(screen.getByRole('button',{name:'Editorial decision: publish, predicted 80%'}))
  fireEvent.change(screen.getByLabelText('Explanation for Editorial decision'),{target:{value:'Updated draft'}})
  expect(onLabel).toHaveBeenCalledWith('publish')
  expect(onExplanation).toHaveBeenCalledWith('Updated draft')
})

it('renders recorded replay state without allowing a mutation',()=>{
  render(<LabelingReview targetId="cycle-18" classifierName="Editorial decision" prediction={prediction} recordedLabel="reject" recordedExplanation="Saved reason" readOnly simulation />)
  expect(screen.getByText('Recorded label: reject · replayed after prediction')).toBeVisible()
  expect(screen.getByRole('button',{name:'Editorial decision: reject'})).toBeDisabled()
  expect(screen.getByLabelText('Explanation for Editorial decision')).toHaveValue('Saved reason')
})
