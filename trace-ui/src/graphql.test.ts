import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {subscribeEvents} from './graphql'

class FakeSocket{
  static OPEN=1
  static instances:FakeSocket[]=[]
  onopen:(()=>void)|null=null
  onmessage:((event:{data:string})=>void)|null=null
  onclose:((event:{code:number})=>void)|null=null
  onerror:(()=>void)|null=null
  readyState=1
  sent:Record<string,unknown>[]=[]
  url:string
  protocol:string
  constructor(url:string,protocol:string){this.url=url;this.protocol=protocol;FakeSocket.instances.push(this)}
  send(value:string){this.sent.push(JSON.parse(value))}
  close(){this.readyState=3;this.onclose?.({code:1000})}
  receive(value:unknown){this.onmessage?.({data:JSON.stringify(value)})}
  open(){this.onopen?.();this.receive({type:'connection_ack'})}
}
const envelope=(sequence:number)=>({id:'events',type:'next',payload:{data:{runEvents:{sequence,sourceId:`event-${sequence}`,payload:{kind:'optimizer-response'}}}}})
beforeEach(()=>{vi.useFakeTimers();FakeSocket.instances=[];vi.stubGlobal('WebSocket',FakeSocket)})
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals()})

test('reconnecting resumes after the last delivered event and discards duplicates without model commands',()=>{
  const deliver=vi.fn(),status=vi.fn()
  const stop=subscribeEvents('run',10,deliver,status)
  const first=FakeSocket.instances[0];first.open();first.receive(envelope(11));first.receive(envelope(11))
  first.close();vi.advanceTimersByTime(1000)
  const second=FakeSocket.instances[1];second.open();second.receive(envelope(11));second.receive(envelope(12))
  expect((second.sent[1].payload as {variables:{after:number}}).variables.after).toBe(11)
  expect(deliver.mock.calls.map(([e])=>e.sequence)).toEqual([11,12])
  expect(status).toHaveBeenCalledWith('Reconnecting')
  expect(FakeSocket.instances.flatMap(socket=>socket.sent).every(message=>!JSON.stringify(message).includes('mutation'))).toBe(true)
  stop()
})

test('a late frame from a closed connection cannot advance the resumed cursor',()=>{
  const deliver=vi.fn()
  const stop=subscribeEvents('run',0,deliver,vi.fn())
  const old=FakeSocket.instances[0];old.open();old.close();vi.advanceTimersByTime(1000)
  const active=FakeSocket.instances[1];active.open()
  old.receive(envelope(100));active.receive(envelope(1))
  expect(deliver.mock.calls.map(([e])=>e.sequence)).toEqual([1])
  stop()
})

test('malformed frames and invalid events never throw or advance the durable cursor',()=>{
  const deliver=vi.fn(),status=vi.fn()
  const stop=subscribeEvents('run',5,deliver,status)
  const socket=FakeSocket.instances[0];socket.open()
  expect(()=>socket.onmessage?.({data:'not json'})).not.toThrow()
  vi.advanceTimersByTime(1000)
  const active=FakeSocket.instances.at(-1)!;active.open()
  active.receive({id:'events',type:'next',payload:{data:{runEvents:{sequence:100,payload:null}}}})
  active.receive(envelope(6))
  expect(deliver.mock.calls.map(([e])=>e.sequence)).toEqual([6])
  stop()
})

test('subscription errors and unexpected completion reconnect instead of remaining falsely connected',()=>{
  const status=vi.fn(),stop=subscribeEvents('run',0,vi.fn(),status)
  const first=FakeSocket.instances[0];first.open();first.receive({id:'events',type:'error',payload:[{message:'private diagnostic'}]})
  expect(status).toHaveBeenLastCalledWith('Reconnecting')
  vi.advanceTimersByTime(1000)
  const second=FakeSocket.instances[1];second.open();second.receive({id:'events',type:'complete'})
  expect(status).toHaveBeenLastCalledWith('Reconnecting')
  expect(JSON.stringify(status.mock.calls)).not.toContain('private diagnostic')
  stop();vi.advanceTimersByTime(1000)
  expect(FakeSocket.instances).toHaveLength(2)
})

test('authorization failures stop automatic connection retries and report the required action',()=>{
  const status=vi.fn(),stop=subscribeEvents('run',0,vi.fn(),status)
  FakeSocket.instances[0].onclose?.({code:4401})
  expect(status).toHaveBeenLastCalledWith('Authentication required')
  vi.advanceTimersByTime(5000)
  expect(FakeSocket.instances).toHaveLength(1)
  stop()
})

test('duplicate acknowledgements do not subscribe twice and cleanup completes only its own stream',()=>{
  const deliver=vi.fn(),stop=subscribeEvents('run',0,deliver,vi.fn())
  const socket=FakeSocket.instances[0];socket.open();socket.receive({type:'connection_ack'})
  expect(socket.sent.filter(message=>message.type==='subscribe')).toHaveLength(1)
  stop();socket.receive(envelope(1));vi.advanceTimersByTime(5000)
  expect(socket.sent.at(-1)).toEqual({id:'events',type:'complete'})
  expect(deliver).not.toHaveBeenCalled()
  expect(FakeSocket.instances).toHaveLength(1)
})

test('a connection without an acknowledgement times out and cleanup cancels both timers',()=>{
  const status=vi.fn(),stop=subscribeEvents('run',3,vi.fn(),status)
  vi.advanceTimersByTime(10000)
  expect(status).toHaveBeenLastCalledWith('Reconnecting')
  vi.advanceTimersByTime(1000)
  expect(FakeSocket.instances).toHaveLength(2)
  stop();vi.advanceTimersByTime(20000)
  expect(FakeSocket.instances).toHaveLength(2)
})
