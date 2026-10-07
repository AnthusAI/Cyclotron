import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {MatchedComparison,MatchedComparisonResult} from './MatchedComparison'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
const runs=[{id:'before',name:'Before'},{id:'after',name:'After'}]
test('comparison preflight makes no paid mutation and requires explicit approval',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>query.includes('createMatchedComparison')?{createMatchedComparison:{id:'comparison'}}:{matchedRunPreflight:{fingerprint:'frozen',sample_count:10,request_upper_bound:20,class_counts:{topic:{yes:5,no:5}},excluded_count:2}})
  const select=vi.fn()
  render(<MatchedComparison runs={runs} initialRunId="after" onCreated={select} onClose={()=>{}} />)
  fireEvent.change(screen.getByLabelText('Before run'),{target:{value:'before'}})
  fireEvent.click(screen.getByRole('button',{name:'Check protected samples'}))
  await screen.findByText('10 matched items · at most 20 requests')
  expect(vi.mocked(graphql).mock.calls).toHaveLength(1)
  expect(screen.getByRole('button',{name:'Run matched comparison'})).toBeDisabled()
  fireEvent.click(screen.getByLabelText('Authorize paid comparison requests'))
  fireEvent.click(screen.getByRole('button',{name:'Run matched comparison'}))
  await waitFor(()=>expect(select).toHaveBeenCalledWith('comparison'))
  expect(vi.mocked(graphql).mock.calls.at(-1)?.[1]).toMatchObject({before:'before',after:'after',fingerprint:'frozen',ceiling:20,confirmed:true})
})
test('changing an endpoint discards approved preflight',async()=>{
  vi.mocked(graphql).mockResolvedValue({matchedRunPreflight:{fingerprint:'old',sample_count:1,request_upper_bound:2,class_counts:{},excluded_count:0}})
  render(<MatchedComparison runs={runs} initialRunId="after" onCreated={()=>{}} onClose={()=>{}} />)
  fireEvent.change(screen.getByLabelText('Before run'),{target:{value:'before'}})
  fireEvent.click(screen.getByRole('button',{name:'Check protected samples'}))
  await screen.findByText('1 matched items · at most 2 requests')
  fireEvent.change(screen.getByLabelText('After run'),{target:{value:'before'}})
  expect(screen.queryByRole('button',{name:'Run matched comparison'})).toBeNull()
})
test('comparison result orders metrics recall precision accuracy and discloses protected scope',()=>{
  render(<MatchedComparisonResult status="completed" result={{sample_count:5,classifiers:{topic:{before:{recall:.4,precision:.5,accuracy:.6},after:{recall:.7,precision:.8,accuracy:.9}}}}} />)
  expect(screen.getAllByRole('columnheader').map(node=>node.textContent)).toEqual(['Endpoint','Recall','Precision','Accuracy','ECE','Brier'])
  expect(screen.getByText(/5 shared protected items/)).toBeVisible()
})
test('comparison identifies macro averaging and undefined class support',()=>{
  const metrics={metric_aggregation:'macro' as const,recall:.5,precision:null,accuracy:.5,undefined_precision_classes:['b']}
  render(<MatchedComparisonResult status="completed" result={{sample_count:5,classifiers:{topic:{before:metrics,after:metrics}}}} />)
  expect(screen.getByText('Recall and precision: macro average across configured classes.')).toBeVisible()
  expect(screen.getByText('Before precision undefined for: b.')).toBeVisible()
  expect(screen.getByText('After precision undefined for: b.')).toBeVisible()
})
test('item inspection fetches only the selected recorded exchange',async()=>{
  vi.mocked(graphql).mockResolvedValue({matchedEvaluationTarget:null})
  render(<MatchedComparisonResult runId="comparison" status="completed" result={{sample_count:1,classifiers:{},records:[{endpoint:'before',classifier_id:'topic',item_id:'paper',actual_label:'yes',label:'no',decision_model_label:'no',trace_event_id:12}]}} />)
  expect(vi.mocked(graphql)).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Inspect before · paper · topic'}))
  await screen.findByText(/No item trace was recorded/)
  expect(vi.mocked(graphql).mock.calls[0][1]).toEqual({run:'comparison',event:12})
})
