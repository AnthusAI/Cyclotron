import {afterEach,expect,test,vi} from 'vitest'
import {consoleAccounts} from './consoleSession'

afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals()})

test('an unanswered account request aborts and exposes a bounded retryable error',async()=>{
  vi.useFakeTimers()
  const fetch=vi.fn((_path:string,options:RequestInit)=>new Promise<Response>((_resolve,reject)=>{
    options.signal!.addEventListener('abort',()=>reject(new DOMException('Aborted','AbortError')))
  }))
  vi.stubGlobal('fetch',fetch)
  const request=consoleAccounts.session()
  const failed=expect(request).rejects.toThrow('Account service took too long. Try again.')
  await vi.advanceTimersByTimeAsync(20000)
  await failed
  expect(fetch).toHaveBeenCalledTimes(1)
})
