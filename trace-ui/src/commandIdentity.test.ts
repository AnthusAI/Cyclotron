import {afterEach,expect,test} from 'vitest'
import {PendingCommandIds} from './commandIdentity'
afterEach(()=>sessionStorage.clear())
test('uncertain commands survive restart while acknowledged commands get fresh identities',()=>{
  const first=new PendingCommandIds()
  const id=first.identity('run','label',{item_id:'paper',label:'yes'})
  const restarted=new PendingCommandIds()
  expect(restarted.identity('run','label',{label:'yes',item_id:'paper'})).toBe(id)
  restarted.acknowledge(id)
  expect(new PendingCommandIds().identity('run','label',{item_id:'paper',label:'yes'})).not.toBe(id)
})
test('run kind and complete nested payload isolate uncertain command identities',()=>{
  const journal=new PendingCommandIds()
  const id=journal.identity('run','label',{labels:[{comment:'reason',label:'yes'}]})
  expect(journal.identity('run','label',{labels:[{label:'yes',comment:'reason'}]})).toBe(id)
  expect(journal.identity('other','label',{labels:[{comment:'reason',label:'yes'}]})).not.toBe(id)
  expect(journal.identity('run','correct',{labels:[{comment:'reason',label:'yes'}]})).not.toBe(id)
  expect(journal.identity('run','label',{labels:[{comment:'changed',label:'yes'}]})).not.toBe(id)
})
test('unavailable storage retains identities in memory without disabling labeling',()=>{
  const storage={getItem:()=>{throw new Error('Unavailable')},setItem:()=>{throw new Error('Unavailable')}}
  const journal=new PendingCommandIds(storage)
  expect(journal.identity('run','optimize',{})).toBe(journal.identity('run','optimize',{}))
})
test('malformed stored command identities cannot disable future submissions',()=>{
  sessionStorage.setItem('cyclotron.commands.v1',JSON.stringify([[JSON.stringify(['run','label',{}]),'']]))
  expect(new PendingCommandIds().identity('run','label',{})).toMatch(/^[0-9a-f-]{36}$/)
})
