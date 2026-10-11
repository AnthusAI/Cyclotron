import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {createCycleSchedule,DEFAULT_FIRST_DELAY_MS,DEFAULT_INTERVAL_MS} from './cycleSchedule'

beforeEach(()=>{vi.useFakeTimers()})
afterEach(()=>{vi.useRealTimers()})

test('the defaults are 3 seconds, then every 10 seconds',()=>{
  expect(DEFAULT_FIRST_DELAY_MS).toBe(3000)
  expect(DEFAULT_INTERVAL_MS).toBe(10000)
})

test('the first cycle comes 3 s after the mark first becomes visible, then one every 10 s',()=>{
  const onCycle=vi.fn()
  const schedule=createCycleSchedule({onCycle})
  vi.advanceTimersByTime(20000)
  expect(onCycle).not.toHaveBeenCalled()
  schedule.setVisible(true)
  vi.advanceTimersByTime(2999)
  expect(onCycle).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1)
  expect(onCycle).toHaveBeenCalledTimes(1)
  vi.advanceTimersByTime(9999)
  expect(onCycle).toHaveBeenCalledTimes(1)
  vi.advanceTimersByTime(1)
  expect(onCycle).toHaveBeenCalledTimes(2)
  vi.advanceTimersByTime(10000)
  expect(onCycle).toHaveBeenCalledTimes(3)
})

test('it pauses while hidden or off screen, and waits a full interval after coming back',()=>{
  const onCycle=vi.fn()
  const schedule=createCycleSchedule({onCycle})
  schedule.setVisible(true)
  vi.advanceTimersByTime(3000)
  expect(onCycle).toHaveBeenCalledTimes(1)
  vi.advanceTimersByTime(5000)
  schedule.setVisible(false)
  vi.advanceTimersByTime(60000)
  expect(onCycle).toHaveBeenCalledTimes(1)
  schedule.setVisible(true)
  vi.advanceTimersByTime(9999)
  expect(onCycle).toHaveBeenCalledTimes(1)
  vi.advanceTimersByTime(1)
  expect(onCycle).toHaveBeenCalledTimes(2)
})

test('the first delay applies once: hiding before the first cycle restarts the wait with the interval',()=>{
  const onCycle=vi.fn()
  const schedule=createCycleSchedule({firstDelayMs:1000,intervalMs:5000,onCycle})
  schedule.setVisible(true)
  vi.advanceTimersByTime(500)
  schedule.setVisible(false)
  schedule.setVisible(true)
  vi.advanceTimersByTime(4999)
  expect(onCycle).not.toHaveBeenCalled()
  vi.advanceTimersByTime(1)
  expect(onCycle).toHaveBeenCalledTimes(1)
})

test('custom delays are honored and dispose stops everything',()=>{
  const onCycle=vi.fn()
  const schedule=createCycleSchedule({firstDelayMs:100,intervalMs:200,onCycle})
  schedule.setVisible(true)
  vi.advanceTimersByTime(100)
  expect(onCycle).toHaveBeenCalledTimes(1)
  schedule.dispose()
  vi.advanceTimersByTime(10000)
  expect(onCycle).toHaveBeenCalledTimes(1)
})
