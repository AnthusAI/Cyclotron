import {afterEach,expect,test} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {RunOutcomes} from './RunOutcomes'
afterEach(cleanup)
test('recorded change exposes quality and calibration without claiming a matched optimization effect',()=>{
  const first={cycle:1,count:1,accuracy:.5,recall:0,precision:null,ece:.4,brier:.6,probabilityCount:1}
  render(<RunOutcomes windows={[{id:'a',classes:[{label:'yes',role:'positive'}],first,latest:{...first,cycle:85,count:85,accuracy:.8,recall:.7,precision:.6,ece:.2,brier:.3,probabilityCount:83}}]}/>)
  expect([...screen.getByRole('table',{name:'Recorded performance change'}).querySelectorAll('tbody th')].map(cell=>cell.textContent)).toEqual(['Recall','Precision','Accuracy','Calibration error (ECE)↓ better','Brier score↓ better'])
  expect(screen.getByText('+30.0 pp')).toBeVisible()
  expect(screen.getByText('−20.0 pp')).toBeVisible()
  expect(screen.getByText(/not a matched test/)).toBeVisible()
  expect(screen.getByText(/83 probability vectors/)).toBeVisible()
})

test('one recorded snapshot does not pretend there is a before and after comparison',()=>{
  const point={cycle:1,count:1,accuracy:1,recall:null,precision:null,ece:null,brier:null,probabilityCount:0}
  render(<RunOutcomes windows={[{id:'a',classes:[],first:point,latest:point}]}/>)
  expect(screen.queryByText('First recorded')).toBeNull()
  expect(screen.queryByText('Change')).toBeNull()
  expect(screen.getAllByText('Unavailable')).toHaveLength(4)
})

test('matched raw and final outcomes lead ahead of the changing-window historical trend',()=>{
  const first={cycle:1,count:1,accuracy:1,recall:0,precision:null,ece:.4,brier:.6,probabilityCount:1}
  const output={accuracy:.5,per_class:{yes:{recall:.4,precision:.3}},calibration:{count:10,ece:.2,brier:.3,bins:[]}}
  render(<RunOutcomes windows={[{id:'a',classes:[{label:'yes',role:'positive'}],first,latest:{...first,cycle:10,count:10,rawFinal:{count:10,missing_raw_count:0,raw:output,final:{...output,accuracy:.8}}}}]}/>)
  const table=screen.getByRole('table',{name:'Same-item raw and final performance'})
  expect(table).toHaveTextContent('Raw decision model')
  expect(table).toHaveTextContent('Final classifier')
  expect(table).toHaveTextContent('+30.0 pp')
  expect(screen.getByText('Recorded review-window trend').closest('details')).not.toHaveAttribute('open')
  expect(screen.getByText(/not evidence of improvement caused by optimization/)).toBeVisible()
})
