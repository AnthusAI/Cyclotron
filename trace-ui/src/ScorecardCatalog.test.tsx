import {render,screen,fireEvent,cleanup} from '@testing-library/react'
import {expect,it,vi,afterEach} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ScorecardCatalog} from './ScorecardCatalog'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks();window.history.replaceState(null,'','/')})

it('names each scorecard member from its pinned classifier revision rather than the latest rename',async()=>{
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('scorecardDefinitions'))return {scorecardDefinitions:[
      {id:'old',name:'Restored scorecard',revision:3,classifiers:[{id:'topic',revision:1}]},
      {id:'new',name:'Current scorecard',revision:1,classifiers:[{id:'topic',revision:2}]},
    ],classifiers:[{id:'topic',name:'Renamed topic',revision:2}]}
    if(query.includes('classifierVersions'))return {classifierVersions:[
      {id:'topic',name:'Original topic',revision:1},
      {id:'topic',name:'Renamed topic',revision:2},
    ]}
    return {}
  })
  render(<ScorecardCatalog/> )
  expect(await screen.findByText('Original topic · classifier revision 1')).toBeVisible()
  expect(screen.getByText('Renamed topic · classifier revision 2')).toBeVisible()
  expect(screen.queryByText('Renamed topic · classifier revision 1')).toBeNull()
  expect(vi.mocked(graphql).mock.calls.filter(([query])=>query.includes('classifierVersions'))).toHaveLength(1)
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

it('restores an exact historical revision with its original classifier configuration in a read-only view',async()=>{
  window.history.replaceState(null,'','/#section=scorecards&scorecard=card&scorecard_revision=1')
  const current={id:'card',name:'New card',revision:2,classifiers:[{id:'a',revision:2}]}
  vi.mocked(graphql).mockImplementation(async(query,variables)=>{
    if(query.includes('scorecardDefinitions'))return {scorecardDefinitions:[current],classifiers:[]}
    if(query.includes('scorecardDefinitionVersions'))return {scorecardDefinitionVersions:[{...current,name:'Old card',revision:1,classifiers:[{id:'a',revision:1}]},current]}
    if(query.includes('scorecardClassifiers'))return {scorecardClassifiers:[{id:'a',name:'Old classifier',revision:variables?.revision??2,config:{question:variables?.revision===1?'Original question':'Changed question',classes:[{label:'yes'},{label:'no'}]}}]}
    return {classifiers:[],itemLists:[]}
  })
  render(<ScorecardCatalog/> )
  expect(await screen.findByText('Original question')).toBeVisible()
  expect(screen.queryByRole('button',{name:'Edit configuration'})).toBeNull()
  expect(screen.queryByRole('button',{name:'Edit scorecard'})).toBeNull()
  expect(screen.getByRole('button',{name:'Use this definition'})).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
  fireEvent.change(screen.getByRole('combobox',{name:'Inspect scorecard revision'}),{target:{value:'2'}})
  expect(await screen.findByRole('button',{name:'Edit configuration'})).toBeVisible()
  expect(new URLSearchParams(window.location.hash.slice(1)).get('scorecard_revision')).toBe('2')
})

it('restores a selected scorecard from its URL and navigates back to the catalog without mutations',async()=>{
  window.history.replaceState(null,'','/#section=scorecards&scorecard=card')
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('scorecardDefinitions'))return {scorecardDefinitions:[{id:'card',name:'Anthus',revision:1,classifiers:[]}],classifiers:[]}
    if(query.includes('scorecardDefinitionVersions'))return {scorecardDefinitionVersions:[]}
    return {classifiers:[],itemLists:[],scorecardClassifiers:[]}
  })
  render(<ScorecardCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'All scorecards'}))
  expect(new URLSearchParams(window.location.hash.slice(1)).has('scorecard')).toBe(false)
  fireEvent.click(await screen.findByRole('button',{name:'View classifiers'}))
  expect(new URLSearchParams(window.location.hash.slice(1)).get('scorecard')).toBe('card')
  window.history.replaceState(null,'','/#section=scorecards')
  fireEvent(window,new PopStateEvent('popstate'))
  expect(await screen.findByRole('button',{name:'View classifiers'})).toBeVisible()
  expect(vi.mocked(graphql).mock.calls.every(([query])=>!query.includes('mutation'))).toBe(true)
})

it('opens only the classifiers pinned by the selected scorecard',async()=>{
  vi.mocked(graphql).mockImplementation(async(query:string)=>{
    const a={id:'a',name:'Knowledge Bases',revision:1,config:{question:'Knowledge?',classes:[{label:'yes'},{label:'no'}]}}
    if(query.includes('scorecardDefinitions'))return {scorecardDefinitions:[{id:'card',name:'Anthus',revision:2,classifiers:[{id:'a',revision:1}]}]}
    if(query.includes('scorecardClassifiers'))return {scorecardClassifiers:[a]}
    return {classifiers:[{...a,revision:99}],itemLists:[]}
  })
  render(<ScorecardCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'View classifiers'}))
  expect(await screen.findByText('Knowledge Bases')).toBeVisible()
  expect(screen.getByText('Revision 1')).toBeVisible()
  expect(screen.queryByText('Revision 99')).toBeNull()
})

it('creates a scorecard with ordered membership without starting model work',async()=>{
  vi.mocked(graphql).mockResolvedValue({scorecardDefinitions:[],classifiers:[{id:'a',name:'Topic',revision:2}],saveScorecardDefinition:{id:'new',name:'New',revision:1,classifiers:[{id:'a',revision:2}]}})
  render(<ScorecardCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'New scorecard'}))
  fireEvent.change(screen.getByLabelText('Scorecard name'),{target:{value:'New'}})
  fireEvent.click(screen.getByRole('button',{name:'Add Topic'}))
  fireEvent.click(screen.getByRole('button',{name:'Save scorecard'}))
  await screen.findByRole('button',{name:'New scorecard'})
  expect(vi.mocked(graphql).mock.calls.some(([query,variables])=>query.includes('saveScorecardDefinition')&&(variables?.classifiers as {revision:number}[]|undefined)?.[0]?.revision===2)).toBe(true)
  expect(vi.mocked(graphql).mock.calls.some(([query])=>query.includes('createRun'))).toBe(false)
})

it('creates a classifier inside a scorecard draft and pins the returned revision',async()=>{
  vi.mocked(graphql).mockImplementation(async(query)=>{
    if(query.includes('saveClassifier'))return {saveClassifier:{id:'topic',name:'Topic',revision:1}}
    if(query.includes('saveScorecardDefinition'))return {saveScorecardDefinition:{id:'card',name:'New',revision:1,classifiers:[{id:'topic',revision:1}]}}
    return {scorecardDefinitions:[],classifiers:[]}
  })
  render(<ScorecardCatalog/> )
  fireEvent.click(await screen.findByRole('button',{name:'New scorecard'}))
  fireEvent.change(screen.getByLabelText('Scorecard name'),{target:{value:'New'}})
  fireEvent.click(screen.getByRole('button',{name:'Create classifier'}))
  fireEvent.change(screen.getByLabelText('Classifier name'),{target:{value:'Topic'}})
  fireEvent.change(screen.getByLabelText('Decision question'),{target:{value:'Relevant?'}})
  fireEvent.change(screen.getByLabelText('Ordered classes (one per line)'),{target:{value:'yes\nno'}})
  fireEvent.change(screen.getByLabelText('Positive class'),{target:{value:'yes'}})
  fireEvent.click(screen.getByRole('button',{name:'Save classifier'}))
  expect(await screen.findByText('Topic · revision 1')).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Save scorecard'}))
  await screen.findByRole('button',{name:'New scorecard'})
  expect(vi.mocked(graphql).mock.calls.find(([q])=>q.includes('saveScorecardDefinition'))?.[1]?.classifiers).toEqual([{id:'topic',revision:1}])
  expect(vi.mocked(graphql).mock.calls.some(([q])=>q.includes('createRun'))).toBe(false)
})
