import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {MatchedItemTrace} from './MatchedItemTrace'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
test('selecting a comparison item loads its recorded request and response without paid work',async()=>{
  vi.mocked(graphql).mockResolvedValue({matchedEvaluationTarget:{endpoint:'before',target_id:'paper',labels:{topic:'yes'},item:{title:'Knowledge paper',abstract:'Full abstract'},request_fingerprint:'request-fp',checkpoint_fingerprint:'checkpoint-fp',request:{state:{target:{title:'Knowledge paper',abstract:'Full abstract'},classifiers:{topic:{rubric:'Accept knowledge base research',examples:[{text:'Example abstract',label:'yes'}]}}},questions:{q0:{instructions:'Classify only target',options:['yes','no']}}},response:{answers:{topic:{decision:{label:'no',probabilities:{yes:.2,no:.8}}}},model:'fake'},outputs:{topic:{actual_label:'yes',label:'no',probabilities:{yes:.2,no:.8}}},exchanges:[]}})
  render(<MatchedItemTrace runId="comparison" eventId={12} onClose={()=>{}} />)
  await screen.findByText('Knowledge paper')
  expect(screen.getByText('Accept knowledge base research')).toBeVisible()
  expect(screen.getByText('Example abstract')).toBeInTheDocument()
  expect(screen.getByRole('heading',{name:'Structured decision request'})).toBeVisible()
  expect(screen.getByRole('heading',{name:'Decision response'})).toBeVisible()
  expect(vi.mocked(graphql).mock.calls).toHaveLength(1)
  expect(vi.mocked(graphql).mock.calls[0][1]).toEqual({run:'comparison',event:12})
})
test('missing older comparison evidence is disclosed rather than reconstructed',async()=>{
  vi.mocked(graphql).mockResolvedValue({matchedEvaluationTarget:null})
  render(<MatchedItemTrace runId="comparison" eventId={12} onClose={()=>{}} />)
  expect(await screen.findByText(/No item trace was recorded/)).toBeVisible()
})
