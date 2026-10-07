import {act,cleanup,renderHook} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import {useCorrectionDraft,type CorrectionDraft} from './correctionDraft'
afterEach(()=>{cleanup();sessionStorage.clear();vi.restoreAllMocks()})
const edit:CorrectionDraft={item:'paper',edits:{paper:{rows:[{id:'vote:a',item:'paper',classifier:'a',label:'yes',comment:'Original',readonly:false}],draft:{a:{label:'no',comment:'My correction'}}}}}

test('a correction draft restores its expected feedback identity after remount and belongs only to its run',()=>{
  const first=renderHook(()=>useCorrectionDraft('first'))
  act(()=>first.result.current[1](edit))
  first.unmount()
  const restored=renderHook(()=>useCorrectionDraft('first'))
  expect(restored.result.current[0]).toEqual(edit)
  const other=renderHook(()=>useCorrectionDraft('other'))
  expect(other.result.current[0]).toEqual({item:'',edits:{}})
})

test('changing run in place cannot copy drafts or apply an old run callback',()=>{
  const hook=renderHook(({run})=>useCorrectionDraft(run),{initialProps:{run:'first'}})
  act(()=>hook.result.current[1](edit))
  const delayed=hook.result.current[1]
  hook.rerender({run:'second'})
  act(()=>delayed(edit))
  expect(hook.result.current[0]).toEqual({item:'',edits:{}})
  hook.rerender({run:'first'})
  expect(hook.result.current[0]).toEqual(edit)
})

test('malformed correction snapshots are ignored and unavailable storage cannot prevent editing',()=>{
  sessionStorage.setItem('cyclotron.correction-draft.v1:bad',JSON.stringify({...edit,edits:{paper:{...edit.edits.paper,rows:[{...edit.edits.paper.rows[0],item:'different'}]}}}))
  expect(renderHook(()=>useCorrectionDraft('bad')).result.current[0]).toEqual({item:'',edits:{}})
  vi.spyOn(Storage.prototype,'getItem').mockImplementation(()=>{throw new Error('unavailable')})
  vi.spyOn(Storage.prototype,'setItem').mockImplementation(()=>{throw new Error('unavailable')})
  const unavailable=renderHook(()=>useCorrectionDraft('private'))
  act(()=>unavailable.result.current[1](edit))
  expect(unavailable.result.current[0]).toEqual(edit)
})
