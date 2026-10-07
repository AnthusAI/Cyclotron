import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import {afterEach, expect, test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ReliabilityCurve} from './ReliabilityCurve'

afterEach(cleanup)

test('calibration provenance explains recorded fit evidence without a JSON dump', () => {
  render(<ReliabilityCurve curve={{count:1,ece:.1,bins:[],version_counts:{'version-one':1},samples:[{
    item_id:'paper-one',source:'calibrated-head',version:'version-one',temperature:1.25,
    prediction_event_id:11,feedback_event_id:12,
    calibration_provenance:{method:'temperature',fit_on:'out_of_fold',training_ids:['trusted-one','trusted-two']},
  }]}} />)
  fireEvent.click(screen.getByText('Snapshot provenance'))
  expect(screen.getByRole('region',{name:'Calibration sample provenance'})).toHaveAttribute('tabindex','0')
  fireEvent.click(screen.getByText('paper-one'))
  expect(screen.getByText('Calibrated ML head')).toBeVisible()
  expect(screen.getByText('Out-of-fold training predictions')).toBeVisible()
  expect(screen.getByText('1.250')).toBeVisible()
  expect(screen.getByText('version-one')).toBeVisible()
  fireEvent.click(screen.getByText('2 trusted training items'))
  expect(screen.getByText('trusted-one')).toBeVisible()
  expect(screen.getByText('trusted-two')).toBeVisible()
  expect(document.querySelector('pre')).toBeNull()
})

test('raw decision passthrough is not described as fitted calibration', () => {
  render(<ReliabilityCurve curve={{count:1,ece:.1,bins:[],samples:[{
    item_id:'raw-paper',source:'decision-passthrough',version:'raw-version',
    temperature:null,calibration_provenance:null,
  }]}} />)
  fireEvent.click(screen.getByText('Snapshot provenance'))
  fireEvent.click(screen.getByText('raw-paper'))
  expect(screen.getByText('Raw decision output')).toBeVisible()
  expect(screen.getByText('No ML-head calibration was applied.')).toBeVisible()
  expect(screen.queryByText('Out-of-fold training predictions')).toBeNull()
})

test('older and malformed provenance remains unavailable rather than guessed', () => {
  render(<ReliabilityCurve curve={{count:2,ece:.1,bins:[],samples:[
    {item_id:'old-paper'}, null, 'unrecognized',
  ]}} />)
  fireEvent.click(screen.getByText('Snapshot provenance'))
  fireEvent.click(screen.getByText('old-paper'))
  expect(screen.getByText('Output source not recorded')).toBeVisible()
  expect(screen.getByText('Calibration provenance not recorded.')).toBeVisible()
  expect(screen.getByText('2 sample records could not be interpreted.')).toBeVisible()
})
