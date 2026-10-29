import {afterEach,beforeEach,expect,test,vi} from 'vitest'
import {pollEvents} from './pollEvents'
beforeEach(()=>vi.useFakeTimers())
afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals()})
const event=(sequence:number)=>({sequence,sourceId:`source-${sequence}`,payload:{kind:'synthetic'}})
const response=(events:unknown[],status=200)=>new Response(JSON.stringify({events}),{status})
test('polling pins account and cursor, deduplicates pages, and never issues a mutation',async()=>{
  const fetcher=vi.fn().mockResolvedValueOnce(response([event(11)])).mockResolvedValueOnce(response([event(11),event(12)]))
  vi.stubGlobal('fetch',fetcher);const deliver=vi.fn(),status=vi.fn()
  const stop=pollEvents('run',10,deliver,status,'alpha')
  await vi.advanceTimersByTimeAsync(0);await vi.advanceTimersByTimeAsync(2000)
  expect(fetcher.mock.calls.map(call=>call[0])).toEqual(['/api/runs/run/events?after=10&limit=100&account=alpha','/api/runs/run/events?after=11&limit=100&account=alpha'])
  expect(deliver.mock.calls.map(([e])=>e.sequence)).toEqual([11,12])
  expect(fetcher.mock.calls.every(call=>call[1].method==='GET')).toBe(true)
  stop()
})
test.each([401,403])('polling stops and clears host authority on HTTP %i',async code=>{
  const fetcher=vi.fn().mockResolvedValue(response([],code));vi.stubGlobal('fetch',fetcher)
  const status=vi.fn(),expired=vi.fn();pollEvents('run',0,vi.fn(),status,'alpha',expired)
  await vi.advanceTimersByTimeAsync(20000)
  expect(fetcher).toHaveBeenCalledTimes(1);expect(expired).toHaveBeenCalledTimes(1)
  expect(status).toHaveBeenLastCalledWith(code===401?'Authentication required':'Event stream access denied')
})
test('unmount aborts in-flight polling and late responses cannot deliver private events',async()=>{
  let complete!:(value:Response)=>void
  const fetcher=vi.fn((_input:RequestInfo|URL,_options?:RequestInit)=>new Promise<Response>(resolve=>{complete=resolve}));vi.stubGlobal('fetch',fetcher)
  const deliver=vi.fn(),stop=pollEvents('run',0,deliver,vi.fn(),'alpha')
  stop();complete(response([event(1)]));await vi.advanceTimersByTimeAsync(10000)
  expect(fetcher.mock.calls[0]?.[1]?.signal?.aborted).toBe(true)
  expect(deliver).not.toHaveBeenCalled();expect(fetcher).toHaveBeenCalledTimes(1)
})
test('outage backs off, rejects malformed pages atomically, and resumes the same cursor',async()=>{
  const fetcher=vi.fn().mockResolvedValueOnce(response([event(1),{sequence:'bad'}])).mockResolvedValueOnce(response([event(1)]))
  vi.stubGlobal('fetch',fetcher);const deliver=vi.fn(),stop=pollEvents('run',0,deliver,vi.fn(),'alpha')
  await vi.advanceTimersByTimeAsync(0);expect(deliver).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(2000);expect(deliver).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls[1][0]).toContain('after=0');stop()
})
