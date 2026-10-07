import {expect,test} from 'vitest'
import {cleanup,render,screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {afterEach} from 'vitest'
import {CyclotronBrand} from './CyclotronBrand'

afterEach(cleanup)

test('the cycle mark stays within the Cyclotron wordmark height',()=>{
  render(<CyclotronBrand />)
  expect(screen.getByText('Cyclotron')).toBeVisible()
  expect(document.querySelector('svg[aria-hidden="true"]')).toHaveAttribute('height','20')
})

test('the cycle mark is centered in the subtitle-wide space beside the wordmark',()=>{
  render(<CyclotronBrand />)
  const layout=screen.getByTestId('cyclotron-brand-layout')
  expect(layout).toHaveClass('cyclotron-brand-layout')
  expect(screen.getByTestId('cyclotron-cycle-mark')).toHaveClass('cyclotron-cycle-mark')
})
