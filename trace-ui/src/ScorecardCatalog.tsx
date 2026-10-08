import {useEffect,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardHeader,CardTitle,CardContent} from '@/components/ui/card'
import {Catalog} from './Catalog'
import {graphql} from './graphql'
import {ScorecardEditor,type Definition,type CatalogClassifier} from './ScorecardEditor'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {ScorecardComparison} from './ScorecardComparison'

const routeRevision=()=>{const value=new URLSearchParams(window.location.hash.slice(1)).get('scorecard_revision');return value&&/^[1-9]\d*$/.test(value)?Number(value):null}
export function ScorecardCatalog(){
  const [cards,setCards]=useState<Definition[]>([]),[selected,setSelectedValue]=useState(()=>new URLSearchParams(window.location.hash.slice(1)).get('scorecard')??''),[error,setError]=useState('')
  const setSelected=(id:string)=>{
    const route=new URLSearchParams(window.location.hash.slice(1))
    if(id)route.set('scorecard',id);else route.delete('scorecard')
    route.delete('scorecard_revision');setInspected(null);setComparing(false)
    route.set('section','scorecards')
    const hash=`#${route}`
    if(window.location.hash!==hash)window.history.pushState(null,'',hash)
    setSelectedValue(id)
  }
  const [revisionNames,setRevisionNames]=useState<Record<string,string>>({})
  const [classifiers,setClassifiers]=useState<CatalogClassifier[]>([]),[editing,setEditing]=useState(false)
  const [history,setHistory]=useState<{scorecardId:string;versions:Definition[];failed?:boolean}|null>(null),[inspected,setInspected]=useState<number|null>(routeRevision)
  const versions=history?.scorecardId===selected?history.versions:[]
  const [comparing,setComparing]=useState(false)
  const inspectRevision=(revision:number)=>{
    const route=new URLSearchParams(window.location.hash.slice(1));route.set('scorecard_revision',String(revision))
    window.history.pushState(null,'',`#${route}`);setInspected(revision)
  }
  useEffect(()=>{
    const restore=()=>{setSelectedValue(new URLSearchParams(window.location.hash.slice(1)).get('scorecard')??'');setInspected(routeRevision());setEditing(false);setComparing(false)}
    window.addEventListener('popstate',restore);window.addEventListener('hashchange',restore)
    return()=>{window.removeEventListener('popstate',restore);window.removeEventListener('hashchange',restore)}
  },[])
  const refresh=()=>graphql<{scorecardDefinitions:Definition[];classifiers?:CatalogClassifier[]}>('{scorecardDefinitions classifiers}').then(result=>{setCards(result.scorecardDefinitions);setClassifiers(result.classifiers??[])}).catch(e=>setError(e.message))
  useEffect(()=>{let cancelled=false;graphql<{scorecardDefinitions:Definition[];classifiers?:CatalogClassifier[]}>('{scorecardDefinitions classifiers}').then(result=>{if(!cancelled){setCards(result.scorecardDefinitions);setClassifiers(result.classifiers??[])}}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[])
  useEffect(()=>{
    let cancelled=false
    const known=Object.fromEntries(classifiers.map(row=>[`${row.id}:${row.revision}`,row.name]))
    const missing=[...new Set(cards.flatMap(card=>card.classifiers.filter(ref=>!known[`${ref.id}:${ref.revision}`]&&!revisionNames[`${ref.id}:${ref.revision}`]).map(ref=>ref.id)))]
    if(missing.length)void Promise.all(missing.map(id=>graphql<{classifierVersions:CatalogClassifier[]}>('query($id:ID!){classifierVersions(classifierId:$id)}',{id}))).then(results=>{
      if(!cancelled)setRevisionNames(previous=>({...previous,...Object.fromEntries(results.flatMap(result=>(result.classifierVersions??[]).map(row=>[`${row.id}:${row.revision}`,row.name])))}))
    }).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[cards,classifiers,revisionNames])
  useEffect(()=>{if(!selected)return;let cancelled=false;graphql<{scorecardDefinitionVersions:Definition[]}>('query($id:ID!){scorecardDefinitionVersions(scorecardId:$id)}',{id:selected}).then(result=>{if(!cancelled)setHistory({scorecardId:selected,versions:result.scorecardDefinitionVersions??[]})}).catch(e=>{if(!cancelled){setHistory({scorecardId:selected,versions:[],failed:true});setError(e.message)}});return()=>{cancelled=true}},[selected,cards])
  const card=cards.find(row=>row.id===selected)
  const names={...revisionNames,...Object.fromEntries(classifiers.map(row=>[`${row.id}:${row.revision}`,row.name]))}
  const historical=versions.find(row=>row.revision===inspected)
  const readOnly=inspected!==null&&inspected!==card?.revision
  const historyResolved=history?.scorecardId===selected
  const available=inspected===null||inspected===card?.revision||!!historical
  return <section className="scorecard-workspace flex min-h-0 flex-1 flex-col overflow-hidden">
    {comparing&&card&&historical?<ScorecardComparison scorecardId={card.id} beforeRevision={card.revision} afterRevision={historical.revision} onClose={()=>setComparing(false)}/>:null}
    <div className="shrink-0 space-y-3 p-5"><div className="flex items-center justify-between"><h1 className="text-xl font-semibold">Scorecards</h1>{!editing?<Button onClick={()=>{setSelected('');setEditing(true)}}>New scorecard</Button>:null}</div>{error?<p role="alert">{error}</p>:null}
      {card?<div className="flex flex-wrap items-center gap-3"><Button variant="outline" onClick={()=>setSelected('')}>All scorecards</Button><h2>{historical?.name??(readOnly?'Scorecard':card.name)} · revision {inspected??card.revision}</h2><p className="text-xs text-muted-foreground">Classifier edits create new scorecard definitions; running sessions keep their pinned versions.</p></div>:null}
    </div>
    {editing?<ScorecardEditor definition={card} classifiers={classifiers} onCancel={()=>setEditing(false)} onSaved={value=>{setEditing(false);setSelected(value.id);refresh()}}/>:card?<>
      <div className="flex shrink-0 flex-wrap gap-2 px-5">{!readOnly?<Button variant="outline" onClick={()=>setEditing(true)}>Edit scorecard</Button>:null}<NativeSelect aria-label="Inspect scorecard revision" value={inspected??card.revision} onChange={e=>inspectRevision(Number(e.target.value))}>{!available&&inspected!==null?<NativeSelectOption value={inspected} disabled>Revision {inspected} · {historyResolved&&!history?.failed?'unavailable':'not loaded'}</NativeSelectOption>:null}{versions.map(row=><NativeSelectOption key={row.revision} value={row.revision}>Revision {row.revision}{row.revision===card.revision?' · active definition':''}</NativeSelectOption>)}</NativeSelect></div>
      {readOnly?<div className="shrink-0 space-y-2 px-5 pt-3"><p className="text-xs text-muted-foreground">Read-only historical definition. Existing runs and learned checkpoints are unchanged.</p>{historical?<div className="flex flex-wrap gap-2"><Button variant="outline" onClick={()=>setComparing(true)}>Compare with active definition</Button><Button onClick={async()=>{try{await graphql('mutation($id:ID!,$revision:Int!){activateScorecardDefinition(scorecardId:$id,revision:$revision)}',{id:card.id,revision:historical.revision});await refresh()}catch(e){setError((e as Error).message)}}}>Use this definition</Button></div>:historyResolved?<><p role="status">{history?.failed?'Scorecard history could not be loaded.':`Scorecard revision ${inspected} is not available.`}</p><Button variant="outline" onClick={()=>inspectRevision(card.revision)}>Return to active definition</Button></>:<p role="status">Loading scorecard revision…</p>}</div>:null}
      {available?<Catalog key={`${card.id}:${inspected??card.revision}`} section="classifiers" scorecardId={card.id} scorecardRevision={inspected??card.revision} readOnly={readOnly} onChanged={refresh}/>:null}
    </>:<div className="grid gap-4 overflow-y-auto p-5 lg:grid-cols-2">{cards.map(row=><Card key={row.id}><CardHeader><CardTitle>{row.name}</CardTitle><p className="text-xs text-muted-foreground">Revision {row.revision}</p></CardHeader><CardContent><ul className="mb-4 space-y-1 text-sm">{row.classifiers.map(ref=><li key={ref.id}>{names[`${ref.id}:${ref.revision}`]??ref.id} · classifier revision {ref.revision}</li>)}</ul><Button onClick={()=>setSelected(row.id)}>View classifiers</Button></CardContent></Card>)}{!cards.length?<p>No scorecards configured yet.</p>:null}</div>}
  </section>
}
