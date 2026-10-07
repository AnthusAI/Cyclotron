import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardVersions} from './ScorecardVersions'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

test('version comparison preserves recall precision accuracy order and supports explicit reversion',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>query.includes('activateScorecardVersion')?{activateScorecardVersion:{id:'old'}}:query.includes('scorecardVersions')?{scorecardVersions:[{revision:1,run_id:'old',classifiers:[{id:'a',name:'A',config:{classes:[{label:'yes',role:'positive'}]}}],metrics:{a:{count:12,accuracy:.8,per_class:{yes:{recall:.7,precision:.6}}}}},{revision:2,run_id:'new',classifiers:[],metrics:{}}]}:{scorecards:[{id:'scorecard',name:'Anthus',active_revision:2,versions:[{revision:1,run_id:'old'},{revision:2,run_id:'new'}]}]})
  const select=vi.fn()
  render(<ScorecardVersions runId="old" onSelect={select} busy={false} />)
  const use=await screen.findByRole('button',{name:'Use this version'})
  expect(screen.getAllByRole('columnheader').map(node=>node.textContent)).toEqual(['Version','Classifier','Recall','Precision','Accuracy','Labels'])
  fireEvent.click(use)
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Use this version'})).toBeNull())
  expect(vi.mocked(graphql).mock.calls.at(-1)?.[1]).toEqual({id:'scorecard',revision:1})
})
