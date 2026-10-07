import {renderHook,act,cleanup} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import {useLabelDraft} from './labelDraft'
afterEach(()=>{cleanup();vi.restoreAllMocks();sessionStorage.clear()})

test('an unsent label and explanation survive navigation but belong to only their presentation',()=>{
  const first=renderHook(()=>useLabelDraft('run:prediction'))
  act(()=>first.result.current[1]({labels:{topic:'yes'},comments:{topic:'Because knowledge bases'}}))
  first.unmount()
  const restored=renderHook(()=>useLabelDraft('run:prediction'))
  expect(restored.result.current[0]).toEqual({labels:{topic:'yes'},comments:{topic:'Because knowledge bases'}})
  const next=renderHook(()=>useLabelDraft('run:other-prediction'))
  expect(next.result.current[0]).toEqual({labels:{},comments:{}})
})

test('malformed storage is ignored and unavailable storage cannot prevent labeling',()=>{
  sessionStorage.setItem('cyclotron.label-draft.v1:bad','invalid')
  const draft=renderHook(()=>useLabelDraft('bad'))
  expect(draft.result.current[0]).toEqual({labels:{},comments:{}})
  vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw new Error('storage disabled')})
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('storage disabled')})
  const unavailable=renderHook(()=>useLabelDraft('private'))
  act(()=>unavailable.result.current[1]({labels:{a:'yes'},comments:{}}))
  expect(unavailable.result.current[0].labels).toEqual({a:'yes'})
})
