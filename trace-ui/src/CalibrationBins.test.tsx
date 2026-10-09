import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {afterEach,expect,it} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {CalibrationBins} from './CalibrationBins'
afterEach(cleanup)

it('does not offer bin details when no supported values were recorded',()=>{
  render(<CalibrationBins outputs={[{name:'Final',bins:[
    {count:0,mean_confidence:null,accuracy:null},
    {count:1,mean_confidence:null,accuracy:1},
  ]}]}/>)
  expect(screen.queryByText('Calibration bins')).toBeNull()
})

it('provides a focusable narrow-view scroll region without opening it by default',()=>{
  render(<CalibrationBins outputs={[{name:'Final',bins:[{count:4,mean_confidence:.75,accuracy:.5}]}]}/>)
  const toggle=screen.getByText('Calibration bins')
  expect(toggle.closest('details')).not.toHaveAttribute('open')
  fireEvent.click(toggle)
  expect(screen.getByRole('region',{name:'Calibration bins'})).toHaveAttribute('tabindex','0')
  expect(screen.getByRole('rowheader',{name:'Final'})).toBeVisible()
})
