import {afterEach,expect,test,vi} from 'vitest'
import {consoleAccounts} from './consoleSession'

afterEach(()=>{vi.useRealTimers();vi.unstubAllGlobals()})

test('a slow account request waits for the answer instead of giving up on a client timer',async()=>{
  vi.useFakeTimers()
  let answer:(response:Response)=>void=()=>{}
  const fetch=vi.fn((_path:string,options:RequestInit)=>{
    expect(options.signal).toBeUndefined()
    return new Promise<Response>(resolve=>{answer=resolve})
  })
  vi.stubGlobal('fetch',fetch)
  const request=consoleAccounts.session()
  await vi.advanceTimersByTimeAsync(120000)
  answer(new Response(JSON.stringify({authenticated:false,loginUrl:'/auth/login'}),{status:200}))
  await expect(request).resolves.toEqual({authenticated:false,loginUrl:'/auth/login'})
  expect(fetch).toHaveBeenCalledTimes(1)
})
