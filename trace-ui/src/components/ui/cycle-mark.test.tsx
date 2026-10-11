import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {act,cleanup,render} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import * as React from 'react'
import {arrowHead,CYCLE_ARC,CYCLE_HALF,CYCLE_PERIMETER,CycleMark,tipAt,type CycleMarkHandle} from './cycle-mark'
import {RefreshCwWide} from './refresh-cw-wide'

afterEach(()=>{cleanup();vi.useRealTimers();vi.unstubAllGlobals()})

const logoMarkup=()=>{
  const {container}=render(<RefreshCwWide size={20} aria-hidden="true" />)
  const html=container.innerHTML
  cleanup()
  return html
}

test('at rest the cycle mark is RefreshCwWide itself',()=>{
  const {container}=render(<CycleMark size={20} aria-hidden="true" />)
  expect(container.innerHTML).toBe(logoMarkup())
})

test('the loop lengths add up: arrow 1 and the drop are half the loop, so half a lap swaps the arrows',()=>{
  expect(CYCLE_PERIMETER).toBeCloseTo(2*CYCLE_HALF,9)
  expect(CYCLE_ARC).toBeGreaterThan(42.7)
  expect(CYCLE_ARC).toBeLessThan(42.9)
  expect(CYCLE_HALF).toBeCloseTo(CYCLE_ARC+4,9)
})

test("arrow 1's tip sits where the logo's arrowhead corner is, pointing down and to the right",()=>{
  const tip=tipAt(CYCLE_ARC)
  expect(tip.x).toBeCloseTo(39,6)
  expect(tip.y).toBeCloseTo(8,6)
  expect(Math.cos(tip.angle)).toBeGreaterThan(0.7)
  expect(Math.sin(tip.angle)).toBeGreaterThan(0.7)
  // The logo's arrowhead: arms to (39,3) and (34,8).
  const arms=arrowHead(tip).match(/-?\d+(\.\d+)?/g)!.map(Number)
  expect(arms[0]).toBeCloseTo(39,2)
  expect(arms[1]).toBeCloseTo(3,2)
  expect(arms[4]).toBeCloseTo(34,2)
  expect(arms[5]).toBeCloseTo(8,2)
})

test("arrow 2 is arrow 1 turned half a turn about the center, and after half a lap the arrows have swapped places",()=>{
  const a=tipAt(CYCLE_ARC)
  const b=tipAt(CYCLE_ARC+CYCLE_HALF)
  expect(b.x).toBeCloseTo(42-a.x,6)
  expect(b.y).toBeCloseTo(24-a.y,6)
  expect(tipAt(CYCLE_ARC+CYCLE_PERIMETER).x).toBeCloseTo(a.x,6)
})

test('a tip stays on the racetrack the whole way round',()=>{
  for(let l=0;l<CYCLE_PERIMETER;l+=0.5){
    const {x,y}=tipAt(l)
    expect(x).toBeGreaterThanOrEqual(2.9)
    expect(x).toBeLessThanOrEqual(39.1)
    expect(y).toBeGreaterThanOrEqual(2.9)
    expect(y).toBeLessThanOrEqual(21.1)
  }
})

beforeEach(()=>{
  vi.useFakeTimers()
  let now=0
  vi.stubGlobal('performance',{now:()=>now})
  vi.stubGlobal('requestAnimationFrame',(cb:FrameRequestCallback)=>setTimeout(()=>{now+=16;cb(now)},16) as unknown as number)
  vi.stubGlobal('cancelAnimationFrame',(id:number)=>clearTimeout(id))
  vi.stubGlobal('matchMedia',()=>({matches:false}))
})

test('cycle() plays one cycle then returns to the logo pose',async()=>{
  const ref=React.createRef<CycleMarkHandle>()
  const {container}=render(<CycleMark ref={ref} size={20} durationMs={1000} aria-hidden="true" />)
  expect(container.querySelector('[data-cycling]')).toBeNull()
  act(()=>ref.current!.cycle())
  expect(container.querySelector('[data-cycling="true"]')).not.toBeNull()
  await act(async()=>{await vi.advanceTimersByTimeAsync(500)})
  expect(container.querySelector('[data-cycling="true"]')).not.toBeNull()
  await act(async()=>{await vi.advanceTimersByTimeAsync(700)})
  expect(container.querySelector('[data-cycling]')).toBeNull()
  expect(container.innerHTML).toBe(logoMarkup())
})

test('a new cycleKey plays a cycle, and the first render does not',async()=>{
  const {container,rerender}=render(<CycleMark size={20} cycleKey={1} durationMs={1000} aria-hidden="true" />)
  expect(container.querySelector('[data-cycling]')).toBeNull()
  rerender(<CycleMark size={20} cycleKey={2} durationMs={1000} aria-hidden="true" />)
  expect(container.querySelector('[data-cycling="true"]')).not.toBeNull()
})

test('under prefers-reduced-motion a cycle does nothing',()=>{
  vi.stubGlobal('matchMedia',()=>({matches:true}))
  const ref=React.createRef<CycleMarkHandle>()
  const {container}=render(<CycleMark ref={ref} size={20} aria-hidden="true" />)
  act(()=>ref.current!.cycle())
  expect(container.querySelector('[data-cycling]')).toBeNull()
})

test('a cycle called while one is running does not start another',async()=>{
  const ref=React.createRef<CycleMarkHandle>()
  const onCycleEnd=vi.fn()
  render(<CycleMark ref={ref} size={20} durationMs={500} onCycleEnd={onCycleEnd} aria-hidden="true" />)
  act(()=>{ref.current!.cycle();ref.current!.cycle()})
  await act(async()=>{await vi.advanceTimersByTimeAsync(2000)})
  expect(onCycleEnd).toHaveBeenCalledTimes(1)
})

test('a development double-run of effects (StrictMode) does not play a cycle at mount',()=>{
  const {container}=render(<React.StrictMode><CycleMark size={20} cycleKey={1} durationMs={1000} aria-hidden="true" /></React.StrictMode>)
  expect(container.querySelector('[data-cycling]')).toBeNull()
})
