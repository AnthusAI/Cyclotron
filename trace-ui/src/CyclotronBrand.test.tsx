import {expect,test} from 'vitest'
import {render,screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {CyclotronBrand} from './CyclotronBrand'

test('the cycle mark stays within the Cyclotron wordmark height',()=>{
  render(<CyclotronBrand />)
  expect(screen.getByText('Cyclotron')).toBeVisible()
  expect(document.querySelector('svg[aria-hidden="true"]')).toHaveAttribute('height','20')
})
