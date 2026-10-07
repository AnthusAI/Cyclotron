import {render,screen,fireEvent,cleanup,waitFor} from '@testing-library/react'
import {it,expect,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardEditor} from './ScorecardEditor'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

it('saves shared settings as a new definition while retaining unedited extension settings',async()=>{
  vi.mocked(graphql).mockResolvedValue({saveScorecardDefinition:{id:'card',revision:2}})
  render(<ScorecardEditor definition={{id:'card',name:'Card',revision:1,classifiers:[{id:'a',revision:1}],settings:{seed:'original',extension:{value:3},selection_policy:{primary:'f1',aggregation:'macro'}}}} classifiers={[{id:'a',name:'A',revision:1}]} onCancel={()=>{}} onSaved={()=>{}}/> )
  fireEvent.change(screen.getByLabelText('Rubric: label changes per trigger'),{target:{value:'2'}})
  fireEvent.change(screen.getByLabelText('Other optimization stages: labels per trigger'),{target:{value:'10'}})
  fireEvent.change(screen.getByLabelText('Primary objective'),{target:{value:'recall'}})
  fireEvent.change(screen.getByLabelText('Secondary objective'),{target:{value:'accuracy'}})
  fireEvent.click(screen.getByRole('button',{name:'Save scorecard'}))
  await waitFor(()=>expect(graphql).toHaveBeenCalledTimes(1))
  expect(vi.mocked(graphql).mock.calls[0][1]?.settings).toEqual({seed:'original',extension:{value:3},rubric_changes_every:2,optimize_every:10,selection_policy:{primary:'recall',secondary:'accuracy',minimum_secondary:null,aggregation:'macro'}})
  expect(vi.mocked(graphql).mock.calls[0][0]).not.toContain('createRun')
})

it('rejects invalid cadence before sending a save mutation',()=>{
  render(<ScorecardEditor definition={{id:'card',name:'Card',revision:1,classifiers:[{id:'a',revision:1}]}} classifiers={[]} onSaved={()=>{}} onCancel={()=>{}}/> )
  fireEvent.change(screen.getByLabelText('Other optimization stages: labels per trigger'),{target:{value:'0'}})
  expect(screen.getByRole('button',{name:'Save scorecard'})).toBeDisabled()
  expect(graphql).not.toHaveBeenCalled()
})
