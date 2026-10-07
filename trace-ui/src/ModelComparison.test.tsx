import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {expect,it,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ModelComparison,PlaybackModelComparison} from './ModelComparison'
afterEach(cleanup)

it('shows a supported point for each output even when only one bin is occupied',()=>{
  const metrics={accuracy:1,per_class:{},calibration:{count:3,ece:.2,bins:[{count:3,mean_confidence:.8,accuracy:1}]}}
  const {container}=render(<ModelComparison comparison={{count:3,missing_raw_count:0,raw:metrics,final:metrics}}/> )
  expect(container.querySelectorAll('svg circle')).toHaveLength(2)
  expect(screen.getByText('Raw decision model: 3 predictions · confidence 80% · correct 100%')).toBeInTheDocument()
  expect(screen.getByText('Final classifier: 3 predictions · confidence 80% · correct 100%')).toBeInTheDocument()
})

it('protected comparison identifies its scope instead of claiming ongoing review',()=>{
  const raw={accuracy:0,per_class:{yes:{recall:0,precision:0}},calibration:{count:1,ece:.8,brier:.64,bins:[]}}
  const final={accuracy:1,per_class:{yes:{recall:1,precision:1}},calibration:{count:1,ece:.2,brier:.04,bins:[]}}
  render(<ModelComparison classes={[{label:'yes',role:'positive'}]} comparison={{count:1,missing_raw_count:0,evaluation_scope:'protected-matched',raw,final}} />)
  expect(screen.getByText('Same 1 protected matched items · frozen versions · no fitting.')).toBeVisible()
  expect(screen.queryByText(/not held-out/)).toBeNull()
  expect(screen.getByRole('rowheader',{name:'Final − raw'})).toBeVisible()
  expect(screen.getByText(/ECE change −60.0 pp/)).toBeVisible()
})

it('compares raw and final on matching items in recall precision accuracy order',()=>{
  const metrics={count:2,accuracy:.5,per_class:{yes:{recall:.25,precision:.75}},calibration:{count:2,ece:.2,bins:[]}}
  render(<ModelComparison classes={[{label:'yes',role:'positive'},{label:'no',role:'negative'}]} comparison={{count:2,missing_raw_count:1,raw:metrics,final:{...metrics,accuracy:1}}}/> )
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
  fireEvent(window,new CustomEvent('flywheel-model-comparison-position',{detail:{Topic:{comparison:{count:2,missing_raw_count:0,raw:metrics,final:metrics},classes:[{label:'yes',role:'positive'},{label:'no',role:'negative'}]}}}))
  expect(screen.getByText(/Same 2 reviewed items/)).toBeInTheDocument()
  fireEvent(window,new CustomEvent('flywheel-model-comparison-position',{detail:{}}))
  expect(screen.queryByText(/Same 2 reviewed items/)).toBeNull()
  expect(screen.getByText(/No matched comparison snapshot/)).toBeInTheDocument()
})
