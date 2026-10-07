import {render,screen,cleanup} from '@testing-library/react'
import {it,expect,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardComparison} from './ScorecardComparison'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

it('shows directional revision membership and setting changes with original questions without activating anything',async()=>{
  vi.mocked(graphql).mockResolvedValue({scorecardDefinitionComparison:{
    before:{name:'Card',revision:2},after:{name:'Card',revision:1},
    changes:{name:null,members:[{id:'a',before:{position:1,revision:2},after:{position:2,revision:1}},{id:'b',before:{position:2,revision:1},after:null}],settings:[{key:'optimizer_model',before_present:true,before:'new-model',after_present:true,after:'old-model'}]},
    classifiers:[{id:'a',before:{name:'Topic',revision:2,config:{question:'New question',classes:[{label:'yes',role:'positive'},{label:'no',role:'negative'}]}},after:{name:'Old topic',revision:1,config:{question:'Old question',classes:[{label:'yes'},{label:'no'}]}}},{id:'b',before:{name:'Gallery',revision:1,config:{question:'Gallery?',classes:[]}},after:null}]}})
  render(<ScorecardComparison scorecardId="card" beforeRevision={2} afterRevision={1} onClose={()=>{}}/> )
  expect(await screen.findByText('New question')).toBeVisible()
  expect(screen.getByText('Old question')).toBeVisible()
  expect(screen.getByText('Not a member')).toBeVisible()
  expect(screen.getByText('optimizer_model')).toBeVisible()
  expect(screen.getByText('new-model')).toBeVisible()
  expect(screen.getByText('old-model')).toBeVisible()
  expect(screen.getByText(/Definition changes, not a performance comparison/)).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})
