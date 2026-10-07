import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {EvaluationDetails} from './EvaluationDetails'
afterEach(cleanup)

test('evaluation detail discloses F1 class support and descriptive uncertainty',()=>{
  render(<EvaluationDetails metrics={{count:3,f1:2/3,accuracy_interval_95:[.2,.94],per_class:{a:{count:2,predicted_count:1,f1:2/3}},accuracy_interval_method:'Wilson 95%; descriptive, not selection-adjusted or a paired effect interval'}} />)
  fireEvent.click(screen.getByText('F1, sample support and uncertainty'))
  expect(screen.getByText('3 scored items · F1: 66.7%')).toBeVisible()
  expect(screen.getByText('Accuracy interval: 20.0%–94.0%')).toBeVisible()
  expect(screen.getByText(/not selection-adjusted/)).toBeVisible()
  expect(screen.getAllByRole('columnheader').map(node=>node.textContent)).toEqual(['Class','Reviewed','Predicted','F1'])
})

test('older reports do not invent missing uncertainty',()=>{
  render(<EvaluationDetails metrics={{count:3}} />)
  fireEvent.click(screen.getByText('F1, sample support and uncertainty'))
  expect(screen.getByText('Accuracy interval: not recorded')).toBeVisible()
})
