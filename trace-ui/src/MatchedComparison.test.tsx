import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {MatchedComparison,MatchedComparisonResult} from './MatchedComparison'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
test('endpoint support follows that endpoint pinned class ordering',()=>{
  const metrics={class_config:[{label:'include',role:'positive'},{label:'exclude',role:'negative'}],per_class:{exclude:{count:2},include:{count:1}}}
  render(<MatchedComparisonResult status="completed" result={{sample_count:3,classifiers:{topic:{before:metrics,after:metrics}}}} />)
  const support=within(screen.getByRole('region',{name:'before evaluation support'}))
  fireEvent.click(support.getByText('F1, sample support and uncertainty'))
  expect(support.getAllByRole('rowheader').map(row=>row.textContent)).toEqual(['include','exclude'])
})
const runs=[{id:'before',name:'Before'},{id:'after',name:'After'}]
test('an unconfirmed creation retries the same approval identity without duplicating paid work',async()=>{
  vi.mocked(graphql).mockResolvedValueOnce({matchedRunPreflight:{fingerprint:'frozen',sample_count:1,request_upper_bound:2,class_counts:{},excluded_count:0}})
    .mockRejectedValueOnce(new Error('Acknowledgement unavailable'))
    .mockResolvedValueOnce({createMatchedComparison:{id:'comparison'}})
  render(<MatchedComparison runs={runs} initialRunId="after" onCreated={()=>{}} onClose={()=>{}} />)
  fireEvent.click(screen.getByRole('button',{name:'Check protected samples'}))
  await screen.findByText('1 matched items · at most 2 requests')
  fireEvent.click(screen.getByLabelText('Authorize paid comparison requests'))
  fireEvent.click(screen.getByRole('button',{name:'Run matched comparison'}))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button',{name:'Run matched comparison'}))
  await waitFor(()=>expect(vi.mocked(graphql).mock.calls).toHaveLength(3))
  const first=vi.mocked(graphql).mock.calls[1][1]
  expect(first?.request).toEqual(expect.any(String))
  expect(vi.mocked(graphql).mock.calls[2][1]).toEqual(first)
})
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
test('each protected endpoint exposes a raw versus final comparison',()=>{
  const output={accuracy:1,per_class:{yes:{recall:1,precision:1}},calibration:{count:1,ece:.1,bins:[]}}
  const metrics={class_config:[{label:'yes',role:'positive'}],decision_model_comparison:{count:1,missing_raw_count:0,evaluation_scope:'protected-matched' as const,raw:output,final:output}}
  render(<MatchedComparisonResult status="completed" result={{sample_count:1,classifiers:{topic:{before:metrics,after:metrics}}}} />)
  fireEvent.click(screen.getByText('Before raw decision model vs final classifier'))
  expect(screen.getAllByText('Same 1 protected matched items · frozen versions · no fitting.')[0]).toBeVisible()
})
test('item inspection fetches only the selected recorded exchange',async()=>{
  vi.mocked(graphql).mockResolvedValue({matchedEvaluationTarget:null})
  render(<MatchedComparisonResult runId="comparison" status="completed" result={{sample_count:1,classifiers:{},records:[{endpoint:'before',classifier_id:'topic',item_id:'paper',actual_label:'yes',label:'no',decision_model_label:'no',trace_event_id:12}]}} />)
  expect(vi.mocked(graphql)).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Inspect before · paper · topic'}))
  await screen.findByText(/No item trace was recorded/)
  expect(vi.mocked(graphql).mock.calls[0][1]).toEqual({run:'comparison',event:12})
})
test('missing historical human labels are unknown not colored or filtered as disagreements',()=>{
  render(<MatchedComparisonResult status="completed" result={{sample_count:1,classifiers:{},records:[{endpoint:'before',classifier_id:'topic',item_id:'unlabeled',label:'yes',decision_model_label:'no'}]}} />)
  expect(screen.getByText('Not recorded')).toBeVisible()
  expect(screen.getByRole('cell',{name:'yes'})).not.toHaveClass('text-destructive')
  fireEvent.click(screen.getByLabelText('Only disagreements'))
  expect(screen.queryByText('unlabeled')).toBeNull()
})
