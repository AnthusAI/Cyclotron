import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {expect,it,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {CyclotronCatalog} from './CyclotronCatalog'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks();window.history.replaceState(null,'','/')})

it('keeps cyclotron and nested classifier controls in the touch-sized workspace',async()=>{
  vi.mocked(graphql).mockResolvedValue({cyclotronDefinitions:[],classifiers:[]})
  render(<CyclotronCatalog/> )
  const create=screen.getByRole('button',{name:'New cyclotron'})
  expect(create.closest('section')).toHaveClass('cyclotron-workspace')
  fireEvent.click(create)
  expect(screen.getByRole('button',{name:'Save cyclotron'}).closest('section')).toHaveClass('cyclotron-workspace')
})

it('an unavailable revision stops loading and does not query or edit an unrelated classifier definition',async()=>{
  window.history.replaceState(null,'','/#section=cyclotrons&cyclotron=card&cyclotron_revision=999')
  const current={id:'card',name:'Current card',revision:2,classifiers:[]}
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[current],classifiers:[]}
    if(query.includes('cyclotronDefinitionVersions'))return {cyclotronDefinitionVersions:[current]}
    return {classifiers:[],itemLists:[],cyclotronClassifiers:[]}
  })
  render(<CyclotronCatalog/> )
  expect(await screen.findByText('Cyclotron revision 999 is not available.')).toBeVisible()
  expect(screen.getByRole('combobox',{name:'Inspect cyclotron revision'})).toHaveValue('999')
  expect(screen.queryByText('Loading cyclotron revision…')).toBeNull()
  expect(screen.queryByRole('button',{name:'Use this definition'})).toBeNull()
  expect(screen.queryByRole('button',{name:'Edit cyclotron'})).toBeNull()
  expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('cyclotronClassifiers'))).toBe(false)
  fireEvent.click(screen.getByRole('button',{name:'Return to active definition'}))
  expect(await screen.findByRole('button',{name:'Edit cyclotron'})).toBeVisible()
  expect(new URLSearchParams(window.location.hash.slice(1)).get('cyclotron_revision')).toBe('2')
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})

it('a failed history request is not presented as loading forever or as a valid historical version',async()=>{
  window.history.replaceState(null,'','/#section=cyclotrons&cyclotron=card&cyclotron_revision=1')
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[{id:'card',name:'Current card',revision:2,classifiers:[]}],classifiers:[]}
    if(query.includes('cyclotronDefinitionVersions'))throw new Error('History request failed')
    return {classifiers:[],itemLists:[],cyclotronClassifiers:[]}
  })
  render(<CyclotronCatalog/> )
  expect(await screen.findByText('Cyclotron history could not be loaded.')).toBeVisible()
  expect(screen.queryByText('Loading cyclotron revision…')).toBeNull()
  expect(screen.queryByRole('button',{name:'Use this definition'})).toBeNull()
  expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('cyclotronClassifiers'))).toBe(false)
})

it('names each cyclotron member from its pinned classifier revision rather than the latest rename',async()=>{
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[
      {id:'old',name:'Restored cyclotron',revision:3,classifiers:[{id:'topic',revision:1}]},
      {id:'new',name:'Current cyclotron',revision:1,classifiers:[{id:'topic',revision:2}]},
    ],classifiers:[{id:'topic',name:'Renamed topic',revision:2}]}
    if(query.includes('classifierVersions'))return {classifierVersions:[
      {id:'topic',name:'Original topic',revision:1},
      {id:'topic',name:'Renamed topic',revision:2},
    ]}
    return {}
  })
  render(<CyclotronCatalog/> )
  expect(await screen.findByText('Original topic · classifier revision 1')).toBeVisible()
  expect(screen.getByText('Renamed topic · classifier revision 2')).toBeVisible()
  expect(screen.queryByText('Renamed topic · classifier revision 1')).toBeNull()
  expect(vi.mocked(graphql).mock.calls.filter(([query])=>query.includes('classifierVersions'))).toHaveLength(1)
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

it('restores an exact historical revision with its original classifier configuration in a read-only view',async()=>{
  window.history.replaceState(null,'','/#section=cyclotrons&cyclotron=card&cyclotron_revision=1')
  const current={id:'card',name:'New card',revision:2,classifiers:[{id:'a',revision:2}]}
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[current],classifiers:[]}
    if(query.includes('cyclotronDefinitionVersions'))return {cyclotronDefinitionVersions:[{...current,name:'Old card',revision:1,classifiers:[{id:'a',revision:1}]},current]}
    if(query.includes('cyclotronClassifiers'))return {cyclotronClassifiers:[{id:'a',name:'Old classifier',revision:variables?.revision??2,config:{question:variables?.revision===1?'Original question':'Changed question',classes:[{label:'yes'},{label:'no'}]}}]}
    return {classifiers:[],itemLists:[]}
  })
  render(<CyclotronCatalog/> )
  expect(await screen.findByText('Original question')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Edit configuration'})).toBeNull()
  expect(screen.queryByRole('button',{name:'Edit cyclotron'})).toBeNull()
  expect(screen.getByRole('button',{name:'Use this definition'})).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
  fireEvent.change(screen.getByRole('combobox',{name:'Inspect cyclotron revision'}),{target:{value:'2'}})
  expect(await screen.findByRole('button',{name:'Edit configuration'})).toBeVisible()
  expect(new URLSearchParams(window.location.hash.slice(1)).get('cyclotron_revision')).toBe('2')
})

it('restores a selected cyclotron from its URL and navigates back to the catalog without mutations',async()=>{
  window.history.replaceState(null,'','/#section=cyclotrons&cyclotron=card')
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[{id:'card',name:'Anthus',revision:1,classifiers:[]}],classifiers:[]}
    if(query.includes('cyclotronDefinitionVersions'))return {cyclotronDefinitionVersions:[]}
    return {classifiers:[],itemLists:[],cyclotronClassifiers:[]}
  })
  render(<CyclotronCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'All cyclotrons'}))
  expect(new URLSearchParams(window.location.hash.slice(1)).has('cyclotron')).toBe(false)
  fireEvent.click(await screen.findByRole('button',{name:'View classifiers'}))
  expect(new URLSearchParams(window.location.hash.slice(1)).get('cyclotron')).toBe('card')
  window.history.replaceState(null,'','/#section=cyclotrons')
  fireEvent(window,new PopStateEvent('popstate'))
  expect(await screen.findByRole('button',{name:'View classifiers'})).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

it('opens only the classifiers pinned by the selected cyclotron',async()=>{
  vi.mocked(graphql).mockImplementation(async(query:string)=>{
    const a={id:'a',name:'Knowledge Bases',revision:1,config:{question:'Knowledge?',classes:[{label:'yes'},{label:'no'}]}}
    if(query.includes('cyclotronDefinitions'))return {cyclotronDefinitions:[{id:'card',name:'Anthus',revision:2,classifiers:[{id:'a',revision:1}]}]}
    if(query.includes('cyclotronClassifiers'))return {cyclotronClassifiers:[a]}
    return {classifiers:[{...a,revision:99}],itemLists:[]}
  })
  render(<CyclotronCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'View classifiers'}))
  expect(await screen.findByText('Knowledge Bases')).toBeVisible()
  expect(screen.getByText('Revision 1')).toBeVisible()
  expect(screen.queryByText('Revision 99')).toBeNull()
})

it('creates a cyclotron with ordered membership without starting model work',async()=>{
  vi.mocked(graphql).mockResolvedValue({cyclotronDefinitions:[],classifiers:[{id:'a',name:'Topic',revision:2}],saveCyclotronDefinition:{id:'new',name:'New',revision:1,classifiers:[{id:'a',revision:2}]}})
  render(<CyclotronCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'New cyclotron'}))
  fireEvent.change(screen.getByLabelText('Cyclotron name'),{target:{value:'New'}})
  fireEvent.click(screen.getByRole('button',{name:'Add Topic'}))
  fireEvent.click(screen.getByRole('button',{name:'Save cyclotron'}))
  await screen.findByRole('button',{name:'New cyclotron'})
  expect(vi.mocked(graphql).mock.calls.some(([query,variables])=>query.includes('saveCyclotronDefinition')&&(variables?.classifiers as {revision:number}[]|undefined)?.[0]?.revision===2)).toBe(true)
  expect(vi.mocked(graphql).mock.calls.some(([query])=>query.includes('createRun'))).toBe(false)
})

it('creates a classifier inside a cyclotron draft and pins the returned revision',async()=>{
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('saveClassifier'))return {saveClassifier:{id:'topic',name:'Topic',revision:1}}
    if(query.includes('saveCyclotronDefinition'))return {saveCyclotronDefinition:{id:'card',name:'New',revision:1,classifiers:[{id:'topic',revision:1}]}}
    return {cyclotronDefinitions:[],classifiers:[]}
  })
  render(<CyclotronCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'New cyclotron'}))
  fireEvent.change(screen.getByLabelText('Cyclotron name'),{target:{value:'New'}})
  fireEvent.click(screen.getByRole('button',{name:'Create classifier'}))
  fireEvent.change(screen.getByLabelText('Classifier name'),{target:{value:'Topic'}})
  fireEvent.change(screen.getByLabelText('Decision question'),{target:{value:'Relevant?'}})
  fireEvent.change(screen.getByLabelText('Ordered classes (one per line)'),{target:{value:'yes\nno'}})
  fireEvent.change(screen.getByLabelText('Positive class'),{target:{value:'yes'}})
  fireEvent.click(screen.getByRole('button',{name:'Save classifier'}))
  expect(await screen.findByText('Topic · revision 1')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Save cyclotron'}))
  await screen.findByRole('button',{name:'New cyclotron'})
  expect(vi.mocked(graphql).mock.calls.find(([q])=>q.includes('saveCyclotronDefinition'))?.[1]?.classifiers).toEqual([{id:'topic',revision:1}])
  expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('createRun'))).toBe(false)
})
