import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react'
import {expect,it,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {Catalog} from './Catalog'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

it('opens classifier history without changing its configuration or starting a run',async()=>{
  const row={id:'a',name:'Topic',revision:2,config:{question:'New question',classes:[{label:'yes'},{label:'no'}]}}
  vi.mocked(graphql).mockImplementation(async(query)=>query.includes('classifierVersions')?{classifierVersions:[{...row,revision:1,config:{...row.config,question:'Old question'}},row]}:{classifiers:[row],itemLists:[]})
  render(<Catalog section="classifiers"/> )
  fireEvent.click(await screen.findByRole('button',{name:'View history'}))
  fireEvent.change(await screen.findByLabelText('Classifier revision'),{target:{value:'1'}})
  expect(await screen.findByText('Old question')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Edit from this revision'}))
  expect(screen.getByLabelText('Decision question')).toHaveValue('Old question')
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

it('keeps classifier save controls outside the scrolling fields',async()=>{
  vi.mocked(graphql).mockResolvedValue({classifiers:[],itemLists:[]})
  render(<Catalog section="classifiers" />)
  fireEvent.click(screen.getByRole('button',{name:'New classifier'}))
  const save=screen.getByRole('button',{name:'Save classifier'})
  expect(screen.getByRole('button',{name:'Save changes'}).closest('[data-slot="card-header"]')).not.toBeNull()
  expect(save.closest('[data-slot="card-footer"]')).not.toBeNull()
  const fields=screen.getByLabelText('Name').closest('[data-slot="card-content"]')
  expect(fields).toHaveClass('min-h-0','overflow-y-auto')
  expect(fields).not.toContainElement(save)
  expect(save.closest('[data-slot="card"]')).toHaveClass('shrink-0')
})

it('shows every active cyclotron affected by a classifier edit before any save',async()=>{
  const row={id:'a',name:'Shared topic',revision:2,config:{question:'Topic?',classes:[{label:'yes'},{label:'no'}]}}
  vi.mocked(graphql).mockImplementation(async(query)=>query.includes('cyclotronDefinitions')?{cyclotronDefinitions:[{id:'one',name:'First card',revision:3,classifiers:[{id:'a',revision:2}]},{id:'two',name:'Second card',revision:7,classifiers:[{id:'a',revision:1}]},{id:'other',name:'Unrelated card',revision:1,classifiers:[{id:'b',revision:1}]}]}:{classifiers:[row],itemLists:[]})
  render(<Catalog section="classifiers"/> )
  fireEvent.click(await screen.findByRole('button',{name:'Edit configuration'}))
  expect(await screen.findByText('First card · revision 3')).toBeVisible()
  expect(screen.getByText('Second card · revision 7')).toBeVisible()
  expect(screen.queryByText('Unrelated card')).toBeNull()
  expect(screen.getByText(/Existing runs keep their pinned definitions/)).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})

it('offers separate class choices and explanation fields for the same item',async()=>{
  vi.mocked(graphql).mockImplementation(async(query:string)=>{
    if(query.includes('listItems'))return {listItems:[{id:'paper',revision:1,occurred_at:'2026-01-01',values:{title:'Paper title',abstract:'Paper abstract'}}]}
    if(query.includes('itemLabels'))return {itemLabels:[]}
    return {classifiers:[{id:'a',name:'Library',revision:1,config:{question:'Include?',classes:[{label:'yes'},{label:'no'}]}},{id:'b',name:'Topic',revision:1,config:{question:'Topic?',classes:[{label:'science'},{label:'sport'},{label:'business'}]}}],itemLists:[{id:'papers',name:'Papers',count:1}]}
  })
  render(<Catalog section="items" />)
  fireEvent.click(await screen.findByRole('button',{name:'Papers · 1 items'}))
  fireEvent.click(await screen.findByRole('button',{name:'Paper title'}))
  expect(await screen.findByText('Include?')).toBeVisible()
  expect(screen.getByText('Topic?')).toBeVisible()
  expect(screen.getAllByLabelText('Explanation (optional)')).toHaveLength(2)
  await waitFor(()=>expect(screen.getByRole('option',{name:'business'})).toBeInTheDocument())
})
