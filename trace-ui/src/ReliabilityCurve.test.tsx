import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {expect,it,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ReliabilityCurve,PlaybackCalibration} from './ReliabilityCurve'
afterEach(cleanup)

it('does not call an unavailable matched head calibration error perfect reliability',()=>{
  const empty={count:0,ece:null,bins:[]}
  render(<ReliabilityCurve curve={{...empty,matched_head_comparison:{raw:empty,calibrated:empty}}}/> )
  fireEvent.click(screen.getByText('Matched ML calibration comparison'))
  expect(screen.getByText(/raw ECE — → calibrated ECE —/)).toBeVisible()
  expect(screen.queryByText(/raw ECE 0.0%/)).toBeNull()
})

it('explains missing probabilities without presenting unobserved confidence as calibration evidence',()=>{
  render(<ReliabilityCurve curve={{count:0,missing_probability_count:3,ece:null,bins:[]}}/> )
  expect(screen.getByText(/3 reviewed predictions have no probability vector/)).toBeVisible()
  expect(screen.getByText(/0 with probabilities/)).toBeVisible()
})

it('shows recorded calibration rather than inventing a curve for missing history',()=>{
  const {rerender}=render(<ReliabilityCurve />)
  expect(screen.getByText(/No calibration snapshot/)).toBeVisible()
  rerender(<ReliabilityCurve curve={{count:12,ece:.1,bins:[{count:12,mean_confidence:.8,accuracy:.7}]}} />)
  expect(screen.getByRole('img',{name:'Confidence calibration curve'})).toBeVisible()
  expect(screen.getByText(/10.0%/)).toBeVisible()
})

it('playback moves calibration forward and backward without using future snapshots',()=>{
  render(<PlaybackCalibration/> )
  const curve={count:10,ece:.2,bins:[]}
  fireEvent(window,new CustomEvent('flywheel-calibration-position',{detail:{Topic:curve}}))
  expect(screen.getByText(/20.0%/)).toBeInTheDocument()
  fireEvent(window,new CustomEvent('flywheel-calibration-position',{detail:{Topic:{...curve,count:20,ece:.1}}}))
  expect(screen.getByText(/10.0%/)).toBeInTheDocument()
  fireEvent(window,new CustomEvent('flywheel-calibration-position',{detail:{}}))
  expect(screen.getByText(/No calibration snapshot/)).toBeInTheDocument()
  expect(screen.queryByText(/10.0%/)).toBeNull()
})
