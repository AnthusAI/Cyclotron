import {expect,test,vi} from 'vitest'
import {act,cleanup,render,screen} from '@testing-library/react'
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

// The animate option: off by default; when on, the first cycle comes 3 s after the logo is first visible, then every 10 s.
function stubEnvironment({reduced=false}:{reduced?:boolean}={}){
  const observers:{callback:IntersectionObserverCallback}[]=[]
  vi.stubGlobal('IntersectionObserver',class{
    callback:IntersectionObserverCallback
    constructor(callback:IntersectionObserverCallback){this.callback=callback;observers.push(this)}
    observe(){}
    disconnect(){}
    unobserve(){}
  })
  vi.stubGlobal('matchMedia',()=>({matches:reduced}))
  let now=0
  vi.stubGlobal('performance',{now:()=>now})
  vi.stubGlobal('requestAnimationFrame',(cb:FrameRequestCallback)=>setTimeout(()=>{now+=16;cb(now)},16) as unknown as number)
  vi.stubGlobal('cancelAnimationFrame',(id:number)=>clearTimeout(id))
  const show=(visible:boolean)=>act(()=>observers.forEach((o)=>o.callback([{isIntersecting:visible} as IntersectionObserverEntry],o as unknown as IntersectionObserver)))
  return {show}
}
const restore=()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()}
const cycling=()=>document.querySelector('[data-cycling="true"]')!==null

test('animate is off by default: the mark never cycles',async()=>{
  vi.useFakeTimers()
  const {show}=stubEnvironment()
  render(<CyclotronBrand />)
  show(true)
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000)})
  expect(cycling()).toBe(false)
  restore()
})

test('animate cycles the mark 3 s after the logo first becomes visible, then every 10 s',async()=>{
  vi.useFakeTimers()
  const {show}=stubEnvironment()
  render(<CyclotronBrand animate />)
  await act(async()=>{await vi.advanceTimersByTimeAsync(20000)})
  expect(cycling()).toBe(false)
  show(true)
  await act(async()=>{await vi.advanceTimersByTimeAsync(2900)})
  expect(cycling()).toBe(false)
  await act(async()=>{await vi.advanceTimersByTimeAsync(300)})
  expect(cycling()).toBe(true)
  await act(async()=>{await vi.advanceTimersByTimeAsync(1500)})
  expect(cycling()).toBe(false)
  await act(async()=>{await vi.advanceTimersByTimeAsync(8000)})
  expect(cycling()).toBe(false)
  await act(async()=>{await vi.advanceTimersByTimeAsync(1000)})
  expect(cycling()).toBe(true)
  restore()
})

test('animate pauses off screen',async()=>{
  vi.useFakeTimers()
  const {show}=stubEnvironment()
  render(<CyclotronBrand animate={{firstDelayMs:1000,intervalMs:5000}} />)
  show(true)
  show(false)
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000)})
  expect(cycling()).toBe(false)
  show(true)
  await act(async()=>{await vi.advanceTimersByTimeAsync(5100)})
  expect(cycling()).toBe(true)
  restore()
})

test('animate never cycles under prefers-reduced-motion',async()=>{
  vi.useFakeTimers()
  const {show}=stubEnvironment({reduced:true})
  render(<CyclotronBrand animate />)
  show(true)
  await act(async()=>{await vi.advanceTimersByTimeAsync(30000)})
  expect(cycling()).toBe(false)
  restore()
})
