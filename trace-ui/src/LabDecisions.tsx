import {useCallback,useEffect,useRef,useState,type ReactNode} from 'react'
import {Activity,Clock,ExternalLink,Flag,Gauge,Inbox,ListFilter,LoaderCircle,RefreshCw,Timer,TriangleAlert,UserCheck,X} from 'lucide-react'
import {Badge} from '@/components/ui/badge'
import {Button} from '@/components/ui/button'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {AppDrawer} from './AppDrawer'
import {TraceDetail} from './TraceDetail'
import {useWorkspaceTransport} from './workspaceTransportContext'
import {
  NO_FILTERS,filterQuery,labAccount,labGet,needsReview,percent,seconds,when,
  type DecisionFilters,type DecisionRow,type DecisionsPage,type Health,type ItemDetail,type OutputDecision,type StandingRun,type TraceRecord,
} from './labApi'

const HEALTH_POLL_MS=15000

function Tile({icon,name,value,detail}:{icon:ReactNode;name:string;value:string;detail?:string}){
  return <Card className="gap-1 py-3" data-testid="health-tile">
    <CardHeader className="px-3 pb-0"><p className="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">{icon}{name}</p></CardHeader>
    <CardContent className="px-3"><p className="font-mono text-lg font-medium">{value}</p>{detail?<p className="text-xs text-muted-foreground">{detail}</p>:null}</CardContent>
  </Card>
}

export function HealthTiles({health}:{health:Health|null}){
  const h=health
  return <section aria-label="Run health" className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
    <Tile icon={<Activity className="size-3.5"/>} name="Last hour" value={h?String(h.decisions.lastHour):'—'} detail="decisions"/>
    <Tile icon={<Clock className="size-3.5"/>} name="Last day" value={h?String(h.decisions.lastDay):'—'} detail="decisions"/>
    <Tile icon={<Timer className="size-3.5"/>} name="Median gap" value={seconds(h?.secondsBetweenDecisions.median)} detail="between decisions, last hour"/>
    <Tile icon={<Gauge className="size-3.5"/>} name="p90 gap" value={seconds(h?.secondsBetweenDecisions.p90)} detail="between decisions, last hour"/>
    <Tile icon={<Inbox className="size-3.5"/>} name="Queue" value={h?String(h.queue.pending+h.queue.running):'—'} detail={h?`${h.queue.pending} waiting · ${h.queue.running} running`:undefined}/>
    <Tile icon={<TriangleAlert className="size-3.5"/>} name="Failed" value={h?String(h.failed.lastDay):'—'} detail="jobs, last day"/>
  </section>
}

function OutputChip({name,output,highlight}:{name:string;output:OutputDecision;highlight:boolean}){
  const waiting=needsReview(output)
  return <span className={`inline-flex max-w-full items-center gap-1 rounded-md border px-1.5 py-0.5 text-xs ${highlight?'border-primary':'border-border'}`}>
    <span className="truncate text-muted-foreground">{name}</span>
    <strong className="font-medium">{output.label??'—'}</strong>
    <span className="font-mono text-muted-foreground">{percent(output.confidence)}</span>
    {waiting?<Flag aria-label="Needs review" className="size-3 text-amber-600"/>:null}
    {output.humanLabel?<UserCheck aria-label={`Labeled ${output.humanLabel}`} className="size-3 text-emerald-600"/>:null}
  </span>
}

function DecisionItem({row,names,output,onOpen}:{row:DecisionRow;names:Record<string,string>;output:string;onOpen:()=>void}){
  return <li><button type="button" onClick={onOpen} className="w-full min-w-0 rounded-lg border border-border bg-card p-3 text-left hover:bg-accent/50">
    <p className="line-clamp-2 break-words text-sm font-medium">{row.item.title??row.item.id}</p>
    <p className="mt-0.5 flex flex-wrap gap-x-2 text-xs text-muted-foreground"><span>{when(row.decidedAt)}</span>{row.item.source?<span>{row.item.source}</span>:null}<span className="break-all">{row.item.id}</span>{row.item.revision?<span>revision {row.item.revision}</span>:null}</p>
    <div className="mt-2 flex flex-wrap gap-1">{Object.entries(row.outputs).map(([id,value])=><OutputChip key={id} name={names[id]??id} output={value} highlight={output===id}/>)}</div>
  </button></li>
}

function Filters({run,filters,onChange}:{run:StandingRun;filters:DecisionFilters;onChange:(filters:DecisionFilters)=>void}){
  const [open,setOpen]=useState(false)
  const set=(key:keyof DecisionFilters)=>(event:{target:{value:string}})=>onChange({...filters,[key]:event.target.value})
  const classes=[...new Set((filters.output?run.outputs.filter(o=>o.id===filters.output):run.outputs).flatMap(o=>o.classes))]
  const active=Object.values(filters).filter(Boolean).length
  return <section aria-label="Decision filters" className="space-y-2">
    <div className="flex flex-wrap items-center gap-2">
      <Button size="sm" variant="outline" aria-expanded={open} onClick={()=>setOpen(!open)}><ListFilter/>Filters{active?` · ${active}`:''}</Button>
      {active?<Button size="sm" variant="ghost" onClick={()=>onChange(NO_FILTERS)}><X/>Clear filters</Button>:null}
    </div>
    {open?<div className="grid grid-cols-1 gap-3 rounded-lg border border-border p-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className="space-y-1"><Label htmlFor="filter-output">Publication output</Label><NativeSelect id="filter-output" className="w-full" value={filters.output} onChange={event=>onChange({...filters,output:event.target.value,label:''})}><NativeSelectOption value="">Any output</NativeSelectOption>{run.outputs.map(o=><NativeSelectOption key={o.id} value={o.id}>{o.name}</NativeSelectOption>)}</NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="filter-label">Label</Label><NativeSelect id="filter-label" className="w-full" value={filters.label} onChange={set('label')}><NativeSelectOption value="">Any label</NativeSelectOption>{classes.map(c=><NativeSelectOption key={c} value={c}>{c}</NativeSelectOption>)}</NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="filter-min">Confidence from (%)</Label><Input id="filter-min" type="number" inputMode="numeric" min={0} max={100} value={filters.minConfidence} onChange={set('minConfidence')}/></div>
      <div className="space-y-1"><Label htmlFor="filter-max">Confidence to (%)</Label><Input id="filter-max" type="number" inputMode="numeric" min={0} max={100} value={filters.maxConfidence} onChange={set('maxConfidence')}/></div>
      <div className="space-y-1"><Label htmlFor="filter-review">Needs review</Label><NativeSelect id="filter-review" className="w-full" value={filters.needsReview} onChange={set('needsReview')}><NativeSelectOption value="">Either</NativeSelectOption><NativeSelectOption value="true">Needs review</NativeSelectOption><NativeSelectOption value="false">Does not need review</NativeSelectOption></NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="filter-labeled">Human label</Label><NativeSelect id="filter-labeled" className="w-full" value={filters.labeled} onChange={set('labeled')}><NativeSelectOption value="">Either</NativeSelectOption><NativeSelectOption value="true">Labeled</NativeSelectOption><NativeSelectOption value="false">Unlabeled</NativeSelectOption></NativeSelect></div>
      <div className="space-y-1"><Label htmlFor="filter-source">Source</Label><Input id="filter-source" value={filters.source} onChange={set('source')} placeholder="Exact source name"/></div>
      <div className="space-y-1"><Label htmlFor="filter-since">From</Label><Input id="filter-since" type="datetime-local" value={filters.since} onChange={set('since')}/></div>
      <div className="space-y-1"><Label htmlFor="filter-until">To</Label><Input id="filter-until" type="datetime-local" value={filters.until} onChange={set('until')}/></div>
      <p className="text-xs text-muted-foreground sm:col-span-2 lg:col-span-4">Filters apply per output: a decision is shown when one output (the chosen one, if any) matches them all. Needs review means routed to review and not yet labeled.</p>
    </div>:null}
  </section>
}

const valueText=(value:unknown)=>typeof value==='string'?value:JSON.stringify(value,null,2)
const asEvent=(record:TraceRecord)=>({sequence:Number(record.event_id??0),sourceId:String(record.event_id??''),payload:record})

function Probabilities({value}:{value:Record<string,number>|null}){
  if(!value)return null
  return <ul className="space-y-1" aria-label="Probabilities">{Object.entries(value).sort((a,b)=>b[1]-a[1]).map(([label,p])=><li key={label} className="grid grid-cols-[minmax(0,6rem)_minmax(0,1fr)_3rem] items-center gap-2 text-xs"><span className="truncate">{label}</span><span className="h-1.5 rounded-full bg-muted"><span className="block h-1.5 rounded-full bg-primary" style={{width:`${Math.round(p*100)}%`}}/></span><span className="text-right font-mono">{percent(p)}</span></li>)}</ul>
}

export function ItemDetailView({detail}:{detail:ItemDetail}){
  const values=detail.item.values
  const current=detail.item.revision
  const {title,url,summary,text,source,...rest}=values as Record<string,unknown>
  return <div className="space-y-4">
    <section aria-label="Item content" className="space-y-2">
      <h2 className="break-words text-base font-semibold">{String(title??detail.item.id)}</h2>
      <p className="flex flex-wrap gap-x-2 text-xs text-muted-foreground"><span className="break-all">{detail.item.id}</span>{current?<span>revision {current}</span>:null}{source?<span>{String(source)}</span>:null}</p>
      {typeof url==='string'?<a className="inline-flex max-w-full items-center gap-1 break-all text-sm text-primary underline-offset-4 hover:underline" href={url} target="_blank" rel="noreferrer noopener"><ExternalLink className="size-3.5 shrink-0"/>{url}</a>:null}
      {summary?<p className="whitespace-pre-wrap break-words text-sm">{valueText(summary)}</p>:null}
      {text?<details className="disclosure" open={!summary}><summary>Full text</summary><p className="whitespace-pre-wrap break-words text-sm">{valueText(text)}</p></details>:null}
      {Object.keys(rest).length?<dl className="space-y-2">{Object.entries(rest).map(([key,value])=><div key={key}><dt className="text-xs capitalize text-muted-foreground">{key.replaceAll('_',' ')}</dt><dd className="whitespace-pre-wrap break-words text-sm">{valueText(value)}</dd></div>)}</dl>:null}
    </section>
    <section aria-label="Revision history" className="space-y-2">
      <h3 className="text-sm font-semibold">Revisions</h3>
      <ol className="space-y-1">{detail.revisions.map(r=><li key={r.revision} className="rounded-md border border-border p-2 text-xs">
        <p className="font-medium">Revision {r.revision}{r.revision===current?' · current':''}{r.revision===detail.decision.itemRevision?' · decided':''}</p>
        <p className="text-muted-foreground">Imported {when(r.importedAt)}{r.provenance?` · ${r.provenance.kind==='machine'?`sent by ${r.provenance.session??'a service'} (${r.provenance.principal})`:`submitted by person ${r.provenance.personId}`} · request ${r.provenance.requestId}`:' · source not recorded'}</p>
        {r.revision!==current?<details className="disclosure"><summary>Values of revision {r.revision}</summary><pre>{JSON.stringify(r.values,null,2)}</pre></details>:null}
      </li>)}</ol>
    </section>
    <section aria-label="Decisions per output" className="space-y-2">
      <h3 className="text-sm font-semibold">Decision</h3>
      <p className="text-xs text-muted-foreground">Decided {when(detail.decision.decidedAt)} · <span className="break-all">{detail.decision.decisionId}</span></p>
      {Object.entries(detail.decision.outputs).map(([id,output])=><Card key={id} className="gap-2 py-3"><CardHeader className="px-3"><CardTitle className="flex flex-wrap items-center gap-2 text-sm">{output.name}<Badge variant="secondary">{output.label??'—'}</Badge><span className="font-mono text-xs text-muted-foreground">{percent(output.confidence)}</span>{output.review.selected?<Badge variant="outline"><Flag/>Routed to review</Badge>:null}</CardTitle></CardHeader>
        <CardContent className="space-y-2 px-3 text-xs">
          <Probabilities value={output.probabilities}/>
          <p className="text-muted-foreground">{output.review.detail||(output.review.selected?'Routed to review.':'Not routed to review.')}{output.review.propensity!=null?` Propensity ${percent(output.review.propensity)}.`:''}</p>
          <p className="text-muted-foreground">Model {output.model??'not recorded'} · version {output.version??'—'}{output.publication?` · publication ${output.publication}`:''}</p>
          {output.composition?<p className="text-muted-foreground">Request: {String(output.composition.examples_shown??'—')} of {String(output.composition.examples_available??'—')} examples shown{output.composition.target_trimmed?' · target trimmed to fit':''}</p>:null}
          {output.composition?<details className="disclosure"><summary>Request composition</summary><pre>{JSON.stringify(output.composition,null,2)}</pre></details>:null}
        </CardContent></Card>)}
    </section>
    <section aria-label="Decision request and response" className="space-y-2">
      <h3 className="text-sm font-semibold">Request and response</h3>
      {Object.entries(detail.exchanges).map(([key,exchange])=><div key={key} className="space-y-2">
        <p className="text-xs text-muted-foreground">One request answered {exchange.outputs.map(id=>detail.decision.outputs[id]?.name??id).join(', ')}.</p>
        {exchange.request?<TraceDetail event={asEvent(exchange.request)}/>:null}
        {exchange.response?<TraceDetail event={asEvent(exchange.response)}/>:<p className="text-xs text-muted-foreground">No response was recorded with this request.</p>}
      </div>)}
      {!Object.keys(detail.exchanges).length?<p className="text-xs text-muted-foreground">No decision request was recorded for this decision. No nearby exchange has been substituted.</p>:null}
    </section>
    <section aria-label="Labels" className="space-y-2">
      <h3 className="text-sm font-semibold">Labels</h3>
      {detail.labels.length?<ul className="space-y-1">{detail.labels.map(label=><li key={label.requestId} className="rounded-md border border-border p-2 text-xs">
        <p className="flex flex-wrap items-center gap-2"><strong className="font-medium">{detail.decision.outputs[label.output]?.name??label.output}</strong><Badge variant="secondary">{label.label}</Badge>{label.retracted?<Badge variant="outline">Retracted</Badge>:null}{label.itemRevision!==current?<Badge variant="outline">Given on revision {label.itemRevision}</Badge>:null}</p>
        <p className="mt-1 text-muted-foreground">{label.source.kind==='newsroom'?`${label.source.editor??'An editor'} at ${label.source.site??'a newsroom'}${label.source.asserted?' (asserted by the site)':''}`:'Lab'} · {when(label.createdAt)}</p>
        {label.comment?<p className="mt-1 whitespace-pre-wrap break-words">{label.comment}</p>:null}
      </li>)}</ul>:<p className="text-xs text-muted-foreground">No labels yet for this item.</p>}
    </section>
  </div>
}

type Feed={rows:DecisionRow[];next:string|null;examined:{from:number;to:number}|null;total:number;top:number|null}
const EMPTY:Feed={rows:[],next:null,examined:null,total:0,top:null}

export function LabDecisions(){
  // Hosted, the workspace transport carries the account chosen in the header; self-served, the lab resolves it.
  const {accountId:hostedAccount}=useWorkspaceTransport()
  const [account,setAccount]=useState(''),[runs,setRuns]=useState<StandingRun[]|null>(null),[runId,setRunId]=useState('')
  const [filters,setFilters]=useState<DecisionFilters>(NO_FILTERS),[feed,setFeed]=useState<Feed>(EMPTY),[loading,setLoading]=useState(false)
  const [health,setHealth]=useState<Health|null>(null),[error,setError]=useState(''),[generation,setGeneration]=useState(0)
  const [open,setOpen]=useState<DecisionRow|null>(null),[detail,setDetail]=useState<ItemDetail|null>(null),[detailError,setDetailError]=useState('')
  const sentinel=useRef<HTMLDivElement|null>(null),request=useRef(0)
  const run=runs?.find(r=>r.runId===runId)??null
  const names=Object.fromEntries((run?.outputs??[]).map(o=>[o.id,o.name]))
  useEffect(()=>{let cancelled=false;
    (hostedAccount?Promise.resolve(hostedAccount):labAccount()).then(found=>{if(cancelled)return;setAccount(found);return labGet<StandingRun[]>(found,'standing-runs').then(list=>{if(!cancelled){setRuns(list);setRunId(previous=>previous||list[0]?.runId||'')}})}).catch(e=>{if(!cancelled)setError((e as Error).message)})
    return()=>{cancelled=true}},[hostedAccount])
  const load=useCallback(async(before:string|null,reset:boolean)=>{
    if(!account||!runId)return
    const ticket=reset?++request.current:request.current
    setLoading(true)
    try{
      const page=await labGet<DecisionsPage>(account,`runs/${runId}/decisions`,{...filterQuery(filters),...(before?{before}:{})})
      if(ticket!==request.current)return
      setFeed(previous=>{const base=reset?EMPTY:previous;return {rows:[...base.rows,...page.decisions],next:page.next,examined:{from:base.examined?.from??page.examined.from,to:page.examined.to},total:page.total,top:base.top??page.examined.from}})
      setError('')
    }catch(e){if(ticket===request.current)setError((e as Error).message)}
    finally{if(ticket===request.current)setLoading(false)}
  },[account,runId,filters])
  useEffect(()=>{setFeed(EMPTY);void load(null,true)},[load,generation])
  useEffect(()=>{
    if(!account||!runId)return
    let cancelled=false,timer:ReturnType<typeof setTimeout>|undefined
    const poll=async()=>{try{const value=await labGet<Health>(account,`runs/${runId}/health`);if(!cancelled)setHealth(value)}catch{/* the tiles keep their last values */}
      if(!cancelled)timer=setTimeout(poll,HEALTH_POLL_MS)}
    void poll()
    return()=>{cancelled=true;clearTimeout(timer)}
  },[account,runId,generation])
  // Follow every page while the end of the list is in view; a filtered page may hold no rows and still move on.
  useEffect(()=>{
    const element=sentinel.current
    if(!element||!feed.next||loading||typeof IntersectionObserver==='undefined')return
    const observer=new IntersectionObserver(entries=>{if(entries.some(entry=>entry.isIntersecting))void load(feed.next,false)},{rootMargin:'400px'})
    observer.observe(element)
    return()=>observer.disconnect()
  },[feed.next,loading,load])
  useEffect(()=>{
    if(!open||!account||!runId)return
    let cancelled=false;setDetail(null);setDetailError('')
    labGet<ItemDetail>(account,`runs/${runId}/items/${encodeURIComponent(open.item.id)}`,{job:open.jobId}).then(value=>{if(!cancelled)setDetail(value)}).catch(e=>{if(!cancelled)setDetailError((e as Error).message)})
    return()=>{cancelled=true}
  },[open,account,runId])
  const newer=health&&feed.top!=null&&!filters.until?Math.max(health.latestPosition-feed.top,0):0
  return <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-y-auto overflow-x-hidden p-4 [&>*]:shrink-0">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <div className="min-w-0"><h1 className="text-xl font-semibold">Decisions</h1><p className="text-xs text-muted-foreground">{run?`${run.name} · standing run · newest first`:'Every decision on a standing run, newest first.'}</p></div>
      <div className="flex flex-wrap items-center gap-2">
        {runs&&runs.length>1?<><Label htmlFor="standing-run" className="sr-only">Standing run</Label><NativeSelect id="standing-run" size="sm" value={runId} onChange={event=>setRunId(event.target.value)}>{runs.map(r=><NativeSelectOption key={r.runId} value={r.runId}>{r.name}</NativeSelectOption>)}</NativeSelect></>:null}
        <Button size="sm" variant="outline" disabled={!run} onClick={()=>setGeneration(n=>n+1)}><RefreshCw/>Refresh</Button>
      </div>
    </div>
    {error?<div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">{error}</div>:null}
    {runs&&!runs.length?<p className="text-sm text-muted-foreground">This account has no standing run.</p>:null}
    {run?<>
      <HealthTiles health={health}/>
      <Filters run={run} filters={filters} onChange={setFilters}/>
      {newer?<Button size="sm" variant="secondary" className="self-start" onClick={()=>setGeneration(n=>n+1)}><RefreshCw/>Show {newer} newer {newer===1?'job':'jobs'}</Button>:null}
      <ol aria-label="Decisions" className="space-y-2">{feed.rows.map(row=><DecisionItem key={row.jobId} row={row} names={names} output={filters.output} onOpen={()=>setOpen(row)}/>)}</ol>
      <div ref={sentinel} className="flex flex-col items-center gap-2 py-2 text-xs text-muted-foreground">
        {feed.examined?<p role="status">{feed.rows.length} shown · searched {feed.examined.from-feed.examined.to+1} of {feed.total} jobs{feed.next?'':' · end of the run'}</p>:null}
        {loading?<p className="flex items-center gap-1"><LoaderCircle className="size-3.5 animate-spin"/>Loading</p>:feed.next?<Button size="sm" variant="outline" onClick={()=>void load(feed.next,false)}>Load more</Button>:null}
        {!loading&&feed.examined&&!feed.rows.length&&!feed.next?<p>No decisions match these filters.</p>:null}
      </div>
    </>:null}
    <AppDrawer title="Item detail" side="right" wide open={open!==null} onOpenChange={value=>{if(!value)setOpen(null)}}>
      {detailError?<p role="alert" className="text-sm text-destructive">{detailError}</p>:detail?<ItemDetailView detail={detail}/>:<p className="flex items-center gap-1 text-sm text-muted-foreground"><LoaderCircle className="size-4 animate-spin"/>Loading item</p>}
    </AppDrawer>
  </main>
}
