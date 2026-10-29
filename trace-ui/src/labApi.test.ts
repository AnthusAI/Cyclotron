import {afterEach,expect,test,vi} from 'vitest'
import {NO_FILTERS,filterQuery,labAccount,labGet,needsReview} from './labApi'

afterEach(()=>{vi.unstubAllGlobals();window.history.replaceState(null,'','/')})

test('only filled-in filters are sent, with confidence as a fraction and local times as instants',()=>{
  expect(filterQuery(NO_FILTERS)).toEqual({})
  const query=filterQuery({...NO_FILTERS,output:'anth-us',label:'include',minConfidence:'60',maxConfidence:'95',labeled:'false',since:'2026-10-28T10:00'})
  expect(query).toMatchObject({output:'anth-us',label:'include',minConfidence:'0.6',maxConfidence:'0.95',labeled:'false'})
  expect(query.since).toBe(new Date('2026-10-28T10:00').toISOString())
  expect(query).not.toHaveProperty('needsReview')
})

test('the lab reads the account pinned on the page, else the session\'s selected account',async()=>{
  const fetch=vi.fn(async()=>new Response(JSON.stringify({authenticated:true,selectedAccountId:'session-account'})))
  vi.stubGlobal('fetch',fetch)
  expect(await labAccount()).toBe('session-account')
  expect(fetch).toHaveBeenCalledWith('/api/session',expect.anything())
  window.history.replaceState(null,'','/?account=pinned-account')
  expect(await labAccount()).toBe('pinned-account')
})

test('requests pin the account in a header and surface the server\'s refusal',async()=>{
  const fetch=vi.fn(async()=>new Response(JSON.stringify({detail:'minConfidence must be a number from 0 to 1'}),{status:400}))
  vi.stubGlobal('fetch',fetch)
  await expect(labGet('acct','runs/r/decisions',{minConfidence:'2'})).rejects.toThrow('minConfidence must be a number from 0 to 1')
  const [url,init]=fetch.mock.calls[0] as unknown as [string,RequestInit]
  expect(url).toBe('/api/lab/runs/r/decisions?minConfidence=2')
  expect((init.headers as Record<string,string>)['X-Cyclotron-Account']).toBe('acct')
})

test('an output needs review while it is routed to review and has no human label',()=>{
  const output={binding:'a',label:'include',confidence:.6,probabilities:null,version:1,review:{selected:true,reason:'program',propensity:1,detail:''}}
  expect(needsReview(output)).toBe(true)
  expect(needsReview({...output,humanLabel:'include'})).toBe(false)
  expect(needsReview({...output,review:{...output.review,selected:false}})).toBe(false)
})
