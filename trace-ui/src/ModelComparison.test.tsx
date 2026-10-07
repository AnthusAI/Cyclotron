import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {expect,it} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ModelComparison,PlaybackModelComparison} from './ModelComparison'

it('compares raw and final on matching items in recall precision accuracy order',()=>{
  const metrics={count:2,accuracy:.5,per_class:{yes:{recall:.25,precision:.75}},calibration:{count:2,ece:.2,bins:[]}}
  render(<ModelComparison positive="yes" comparison={{count:2,missing_raw_count:1,raw:metrics,final:{...metrics,accuracy:1}}}/> )
  expect(screen.getAllByRole('columnheader').map(node=>node.textContent)).toEqual(['Output','Recall','Precision','Accuracy'])
  expect(screen.getByRole('rowheader',{name:'Raw decision model'})).toBeVisible()
  expect(screen.getByRole('rowheader',{name:'Final classifier'})).toBeVisible()
  expect(screen.getByText('100.0%')).toBeVisible()
  expect(screen.getByText(/1 older reviewed item/)).toBeVisible()
  expect(screen.getByRole('img',{name:'Raw decision model versus final classifier calibration'})).toBeVisible()
})

it('playback replaces comparisons on reverse seek rather than retaining a future result',()=>{
  cleanup()
  render(<PlaybackModelComparison/> )
  const metrics={accuracy:.5,per_class:{yes:{recall:.25,precision:.75}},calibration:{count:2,ece:.2,bins:[]}}
  fireEvent(window,new CustomEvent('flywheel-model-comparison-position',{detail:{Topic:{comparison:{count:2,missing_raw_count:0,raw:metrics,final:metrics},positive:'yes'}}}))
  expect(screen.getByText(/Same 2 reviewed items/)).toBeInTheDocument()
  fireEvent(window,new CustomEvent('flywheel-model-comparison-position',{detail:{}}))
  expect(screen.queryByText(/Same 2 reviewed items/)).toBeNull()
  expect(screen.getByText(/No matched comparison snapshot/)).toBeInTheDocument()
})
