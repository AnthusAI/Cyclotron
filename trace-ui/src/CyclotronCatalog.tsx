import {useEffect,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardHeader,CardTitle,CardContent} from '@/components/ui/card'
import {Catalog} from './Catalog'
import {useWorkspaceTransport} from './workspaceTransportContext'
import {CyclotronEditor,type Definition,type CatalogClassifier} from './CyclotronEditor'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {CyclotronComparison} from './CyclotronComparison'
import {ChevronRight,CircleCheck} from 'lucide-react'
import {labGet,type StandingRun} from './labApi'

const routeRevision=()=>{const value=new URLSearchParams(window.location.hash.slice(1)).get('cyclotron_revision');return value&&/^[1-9]\d*$/.test(value)?Number(value):null}
export function CyclotronCatalog(){
  const {graphql,accountId}=useWorkspaceTransport()
  // A hosted account's standing publication runs, each pinned to one cyclotron definition; null for a local workspace.
  const [standing,setStanding]=useState<StandingRun[]|null>(null)
  useEffect(()=>{if(!accountId)return;let cancelled=false;labGet<StandingRun[]>(accountId,'standing-runs').then(list=>{if(!cancelled)setStanding(list)}).catch(e=>{if(!cancelled)setError((e as Error).message)});return()=>{cancelled=true}},[accountId])
  const [cards,setCards]=useState<Definition[]>([]),[selected,setSelectedValue]=useState(()=>new URLSearchParams(window.location.hash.slice(1)).get('cyclotron')??''),[error,setError]=useState('')
  const setSelected=(id:string)=>{
    const route=new URLSearchParams(window.location.hash.slice(1))
    if(id)route.set('cyclotron',id);else route.delete('cyclotron')
    route.delete('cyclotron_revision');setInspected(null);setComparing(false)
    route.set('section','cyclotrons')
    const hash=`#${route}`
    if(window.location.hash!==hash)window.history.pushState(null,'',hash)
    setSelectedValue(id)
  }
  const [revisionNames,setRevisionNames]=useState<Record<string,string>>({})
  const [classifiers,setClassifiers]=useState<CatalogClassifier[]>([]),[editing,setEditing]=useState(false)
  const [history,setHistory]=useState<{cyclotronId:string;versions:Definition[];failed?:boolean}|null>(null),[inspected,setInspected]=useState<number|null>(routeRevision)
  const versions=history?.cyclotronId===selected?history.versions:[]
  const [comparing,setComparing]=useState(false)
  const inspectRevision=(revision:number)=>{
    const route=new URLSearchParams(window.location.hash.slice(1));route.set('cyclotron_revision',String(revision))
    window.history.pushState(null,'',`#${route}`);setInspected(revision)
  }
  useEffect(()=>{
    const restore=()=>{setSelectedValue(new URLSearchParams(window.location.hash.slice(1)).get('cyclotron')??'');setInspected(routeRevision());setEditing(false);setComparing(false)}
    window.addEventListener('popstate',restore);window.addEventListener('hashchange',restore)
    return()=>{window.removeEventListener('popstate',restore);window.removeEventListener('hashchange',restore)}
  },[])
  const refresh=()=>graphql<{cyclotronDefinitions:Definition[];classifiers?:CatalogClassifier[]}>('{cyclotronDefinitions classifiers}').then(result=>{setCards(result.cyclotronDefinitions);setClassifiers(result.classifiers??[])}).catch(e=>setError(e.message))
  useEffect(()=>{let cancelled=false;graphql<{cyclotronDefinitions:Definition[];classifiers?:CatalogClassifier[]}>('{cyclotronDefinitions classifiers}').then(result=>{if(!cancelled){setCards(result.cyclotronDefinitions);setClassifiers(result.classifiers??[])}}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[graphql])
  useEffect(()=>{
    let cancelled=false
    const known=Object.fromEntries(classifiers.map(row=>[`${row.id}:${row.revision}`,row.name]))
    const missing=[...new Set(cards.flatMap(card=>card.classifiers.filter(ref=>!known[`${ref.id}:${ref.revision}`]&&!revisionNames[`${ref.id}:${ref.revision}`]).map(ref=>ref.id)))]
    if(missing.length)void Promise.all(missing.map(id=>graphql<{classifierVersions:CatalogClassifier[]}>('query($id:ID!){classifierVersions(classifierId:$id)}',{id}))).then(results=>{
      if(!cancelled)setRevisionNames(previous=>({...previous,...Object.fromEntries(results.flatMap(result=>(result.classifierVersions??[]).map(row=>[`${row.id}:${row.revision}`,row.name])))}))
    }).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[cards,classifiers,revisionNames,graphql])
  useEffect(()=>{if(!selected)return;let cancelled=false;graphql<{cyclotronDefinitionVersions:Definition[]}>('query($id:ID!){cyclotronDefinitionVersions(cyclotronId:$id)}',{id:selected}).then(result=>{if(!cancelled)setHistory({cyclotronId:selected,versions:result.cyclotronDefinitionVersions??[]})}).catch(e=>{if(!cancelled){setHistory({cyclotronId:selected,versions:[],failed:true});setError(e.message)}});return()=>{cancelled=true}},[selected,cards,graphql])
  const card=cards.find(row=>row.id===selected)
  const names={...revisionNames,...Object.fromEntries(classifiers.map(row=>[`${row.id}:${row.revision}`,row.name]))}
  const historical=versions.find(row=>row.revision===inspected)
  const uses=(id:string)=>(standing??[]).filter(run=>run.cyclotron?.id===id)
  const inUse=cards.filter(row=>uses(row.id).length),unused=cards.filter(row=>!uses(row.id).length)
  const cardFor=(row:Definition)=><Card key={row.id}><CardHeader><CardTitle>{row.name}</CardTitle><p className="text-xs text-muted-foreground">Revision {row.revision}</p>
    {uses(row.id).map(run=><p key={run.runId} className="flex items-start gap-1.5 text-xs"><CircleCheck aria-hidden className="mt-px size-3.5 shrink-0 text-emerald-600"/><span>Decides for {run.publications.map(p=>p.id).join(', ')} · {run.name}{run.cyclotron&&run.cyclotron.revision!==row.revision?` · pinned to revision ${run.cyclotron.revision}`:''}</span></p>)}
  </CardHeader><CardContent><ul className="mb-4 space-y-1 text-sm">{row.classifiers.map(ref=><li key={ref.id}>{names[`${ref.id}:${ref.revision}`]??ref.id} · classifier revision {ref.revision}</li>)}</ul><Button onClick={()=>setSelected(row.id)}>View classifiers</Button></CardContent></Card>
  const readOnly=inspected!==null&&inspected!==card?.revision
  const historyResolved=history?.cyclotronId===selected
  const available=inspected===null||inspected===card?.revision||!!historical
  return <section className="cyclotron-workspace flex min-h-0 flex-1 flex-col overflow-hidden">
    {comparing&&card&&historical?<CyclotronComparison cyclotronId={card.id} beforeRevision={card.revision} afterRevision={historical.revision} onClose={()=>setComparing(false)}/>:null}
    <div className="shrink-0 space-y-3 p-5"><div className="flex items-center justify-between"><h1 className="text-xl font-semibold">Cyclotrons</h1>{!editing?<Button onClick={()=>{setSelected('');setEditing(true)}}>New cyclotron</Button>:null}</div>{error?<p role="alert">{error}</p>:null}
      {card?<div className="flex flex-wrap items-center gap-3"><Button variant="outline" onClick={()=>setSelected('')}>All cyclotrons</Button><h2>{historical?.name??(readOnly?'Cyclotron':card.name)} · revision {inspected??card.revision}</h2><p className="text-xs text-muted-foreground">Classifier edits create new cyclotron definitions; running sessions keep their pinned versions.</p></div>:null}
    </div>
    {editing?<CyclotronEditor definition={card} classifiers={classifiers} onCancel={()=>setEditing(false)} onSaved={value=>{setEditing(false);setSelected(value.id);refresh()}}/>:card?<>
      <div className="flex shrink-0 flex-wrap gap-2 px-5">{!readOnly?<Button variant="outline" onClick={()=>setEditing(true)}>Edit cyclotron</Button>:null}<NativeSelect aria-label="Inspect cyclotron revision" value={inspected??card.revision} onChange={e=>inspectRevision(Number(e.target.value))}>{!available&&inspected!==null?<NativeSelectOption value={inspected} disabled>Revision {inspected} · {historyResolved&&!history?.failed?'unavailable':'not loaded'}</NativeSelectOption>:null}{versions.map(row=><NativeSelectOption key={row.revision} value={row.revision}>Revision {row.revision}{row.revision===card.revision?' · active definition':''}</NativeSelectOption>)}</NativeSelect></div>
      {readOnly?<div className="shrink-0 space-y-2 px-5 pt-3"><p className="text-xs text-muted-foreground">Read-only historical definition. Existing runs and learned checkpoints are unchanged.</p>{historical?<div className="flex flex-wrap gap-2"><Button variant="outline" onClick={()=>setComparing(true)}>Compare with active definition</Button><Button onClick={async()=>{try{await graphql('mutation($id:ID!,$revision:Int!){activateCyclotronDefinition(cyclotronId:$id,revision:$revision)}',{id:card.id,revision:historical.revision});await refresh()}catch(e){setError((e as Error).message)}}}>Use this definition</Button></div>:historyResolved?<><p role="status">{history?.failed?'Cyclotron history could not be loaded.':`Cyclotron revision ${inspected} is not available.`}</p><Button variant="outline" onClick={()=>inspectRevision(card.revision)}>Return to active definition</Button></>:<p role="status">Loading cyclotron revision…</p>}</div>:null}
      {available?<Catalog key={`${card.id}:${inspected??card.revision}`} section="classifiers" cyclotronId={card.id} cyclotronRevision={inspected??card.revision} readOnly={readOnly} onChanged={refresh}/>:null}
    </>:<div className="overflow-y-auto p-5">{!cards.length?<p>No cyclotrons configured yet.</p>:standing===null?<div className="grid gap-4 lg:grid-cols-2">{cards.map(cardFor)}</div>:<>
      <div className="grid gap-4 lg:grid-cols-2">{inUse.map(cardFor)}</div>{!inUse.length?<p className="text-sm text-muted-foreground">No standing publication run uses a cyclotron in this account.</p>:null}
      {unused.length?<details className="group mt-6"><summary className="flex cursor-pointer list-none items-center gap-2 text-sm font-medium"><ChevronRight aria-hidden className="size-4 transition-transform group-open:rotate-90"/>Not used by a standing publication run ({unused.length})</summary><p className="mt-2 text-xs text-muted-foreground">Earlier cyclotrons a criteria change replaced, and any not yet bound to a publication. Nothing decides with them.</p><div className="mt-3 grid gap-4 lg:grid-cols-2">{unused.map(cardFor)}</div></details>:null}
    </>}</div>}
  </section>
}
