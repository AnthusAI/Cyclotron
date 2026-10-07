import {cleanup,render,screen,fireEvent} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ClassifierHistory} from './ClassifierHistory'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

test('old classifier revisions are read only until explicitly copied into an editor',async()=>{
  const old={id:'a',name:'Old topic',revision:1,config:{question:'Old question',classes:[{label:'yes',role:'positive'},{label:'no',role:'negative'}]}}
  const current={...old,name:'New topic',revision:2,config:{...old.config,question:'New question'}}
  vi.mocked(graphql).mockResolvedValue({classifierVersions:[old,current]})
  const edit=vi.fn()
  render(<ClassifierHistory classifier={current} onClose={()=>{}} onEdit={edit}/>)
  expect(await screen.findByText('New question')).toBeVisible()
  fireEvent.change(screen.getByLabelText('Classifier revision'),{target:{value:'1'}})
  expect(screen.getByText('Old question')).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
  fireEvent.click(screen.getByRole('button',{name:'Edit from this revision'}))
  expect(edit).toHaveBeenCalledWith(old)
  expect(screen.getByRole('button',{name:'Close Classifier history'})).toBeVisible()
})
