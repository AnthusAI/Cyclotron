import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {CyclotronVersions} from './CyclotronVersions'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

test('version comparison preserves recall precision accuracy order and supports explicit reversion',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>query.includes('activateCyclotronVersion')?{activateCyclotronVersion:{id:'old'}}:query.includes('cyclotronVersions')?{cyclotronVersions:[{revision:1,run_id:'old',classifiers:[{id:'a',name:'A',config:{classes:[{label:'yes',role:'positive'}]}}],metrics:{a:{count:12,accuracy:.8,per_class:{yes:{recall:.7,precision:.6}}}}},{revision:2,run_id:'new',classifiers:[],metrics:{}}]}:{cyclotrons:[{id:'cyclotron',name:'Anthus',active_revision:2,versions:[{revision:1,run_id:'old'},{revision:2,run_id:'new'}]}]})
  const select=vi.fn()
  render(<CyclotronVersions runId="old" onSelect={select} busy={false} />)
  const use=await screen.findByRole('button',{name:'Use this version'})
  expect(screen.getAllByRole('columnheader').map(node=>node.textContent)).toEqual(['Version','Classifier','Recall','Precision','Accuracy','Labels'])
  fireEvent.click(use)
  await waitFor(()=>expect(screen.queryByRole('button',{name:'Use this version'})).toBeNull())
  expect(vi.mocked(graphql).mock.calls.at(-1)?.[1]).toEqual({id:'cyclotron',revision:1})
})

test('inspecting another learned version is navigation only and clearly differs from activation',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>query.includes('cyclotronVersions')?{cyclotronVersions:[{revision:1,run_id:'old',classifiers:[],metrics:{}},{revision:2,run_id:'new',classifiers:[],metrics:{}}]}:{cyclotrons:[{id:'card',name:'Card',active_revision:2,versions:[{revision:1,run_id:'old'},{revision:2,run_id:'new'}]}]})
  const select=vi.fn()
  const {rerender}=render(<CyclotronVersions runId="new" onSelect={select} busy={false}/> )
  expect(await screen.findByText('Active version')).toBeVisible()
  fireEvent.change(screen.getByRole('combobox',{name:'Cyclotron version'}),{target:{value:'old'}})
  expect(select).toHaveBeenCalledWith('old')
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
  rerender(<CyclotronVersions runId="old" onSelect={select} busy={false}/> )
  expect(await screen.findByText('Inspecting inactive version')).toBeVisible()
  expect(await screen.findByRole('button',{name:'Use this version'})).toBeEnabled()
})

test('an unavailable learned edition does not offer activation with an undefined revision',async()=>{
  vi.mocked(graphql).mockImplementation(async query=>query.includes('cyclotronVersions')?{cyclotronVersions:[]}:{cyclotrons:[{id:'card',name:'Card',active_revision:2,versions:[{revision:1,run_id:'old'}]}]})
  render(<CyclotronVersions runId="old" onSelect={()=>{}} busy={false}/> )
  expect(await screen.findByText('This learned version is unavailable.')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Use this version'})).toBeNull()
})
