import {act,cleanup,renderHook} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import {useSessionDraft} from './sessionDraft'
afterEach(()=>{cleanup();sessionStorage.clear()})
const empty=()=>''
const valid=(value:unknown):value is string=>typeof value==='string'
test('local intent is restored only from its validated session storage key',()=>{
  sessionStorage.setItem('wrong','42')
  expect(renderHook(()=>useSessionDraft('wrong',empty,valid)).result.current[0]).toBe('')
  const original=renderHook(()=>useSessionDraft('own',empty,valid))
  act(()=>original.result.current[1]('An unsent note'))
  original.unmount()
  expect(renderHook(()=>useSessionDraft('own',empty,valid)).result.current[0]).toBe('An unsent note')
})
