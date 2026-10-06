import {useCallback,useEffect,useState} from 'react'
import {Activity, History, Plus, RefreshCw, Undo2} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Badge} from '@/components/ui/badge'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {LabelCard,type CurrentItem} from './LabelCard'
import {graphql,subscribeEvents,type TraceEvent} from './graphql'

type Run={id:string;name:string;mode:string;status:string;createdAt:string;config:{selection_policy?:{primary:string;secondary?:string};max_requests?:number}}
type Job={id:string;kind:string;status:string;result:Record<string,unknown>|null}
const RUNS='{runs{id name mode status createdAt config} capabilities{liveEnabled itemCount}}'
const DETAILS='query($id:ID!){run(runId:$id){id name mode status createdAt config} currentItem(runId:$id) jobs(runId:$id){id kind status result}}'
const readable=(value:unknown)=>{
  if(typeof value==='string'){try{return JSON.stringify(JSON.parse(value),null,2)}catch{return value}}
  return JSON.stringify(value,null,2)
}

export function TraceDetail({event}:{event:TraceEvent}){
  const value=event.payload
  const messages=Array.isArray(value.messages)?value.messages as {role:string;content:unknown}[]:null
  return <Card className="gap-2"><CardHeader><CardTitle className="text-sm capitalize">{String(value.kind).replaceAll('-',' ')}</CardTitle><p className="text-xs text-muted-foreground">Event {String(value.event_id??event.sequence)} · {String(value.created_at??'')}</p></CardHeader><CardContent className="space-y-3">
    {messages?messages.map((message,index)=><details key={index} className="disclosure" open={index===messages.length-1}><summary>{message.role} · full request context</summary><pre>{readable(message.content)}</pre></details>):null}
    {value.state?<details className="disclosure" open><summary>Decision state (actual examples and rubric)</summary><pre>{readable(value.state)}</pre></details>:null}
    {value.questions?<details className="disclosure"><summary>Classification questions</summary><pre>{readable(value.questions)}</pre></details>:null}
    {value.content?<details className="disclosure" open><summary>Model response</summary><pre>{readable(value.content)}</pre></details>:null}
    {value.answers?<details className="disclosure" open><summary>Decision answers</summary><pre>{readable(value.answers)}</pre></details>:null}
    {value.tool_calls?<details className="disclosure"><summary>Tool calls</summary><pre>{readable(value.tool_calls)}</pre></details>:null}
    <details className="disclosure"><summary>Exact stored event</summary><pre>{readable(value)}</pre></details>
  </CardContent></Card>
}

export function WebApp(){
  const [runs,setRuns]=useState<Run[]>([]),[selected,setSelected]=useState(''),[run,setRun]=useState<Run|null>(null)
  const [current,setCurrent]=useState<CurrentItem|null>(null),[jobs,setJobs]=useState<Job[]>([]),[events,setEvents]=useState<TraceEvent[]>([])
  const [view,setView]=useState<'timeline'|'label'>('timeline'),[detail,setDetail]=useState<TraceEvent|null>(null),[revision,setRevision]=useState(0)
  const [error,setError]=useState(''),[stream,setStream]=useState('Connecting'),[sending,setSending]=useState(false)
  const [capabilities,setCapabilities]=useState({liveEnabled:false,itemCount:0})
  const [creating,setCreating]=useState(false),[name,setName]=useState(''),[objective,setObjective]=useState('f1'),[confirmed,setConfirmed]=useState(false)
  const [maxRequests,setMaxRequests]=useState(500),[maxOptimizer,setMaxOptimizer]=useState(10),[token,setToken]=useState('')
  const refresh=useCallback(async()=>{
    const result=await graphql<{runs:Run[];capabilities:{liveEnabled:boolean;itemCount:number}}>(RUNS)
    setRuns(result.runs);setCapabilities(result.capabilities)
  },[])
  useEffect(()=>{refresh().catch(e=>setError(e.message))},[refresh])
  useEffect(()=>{
    if(!selected){setRun(null);return}
    let cancelled=false,unsubscribe:(()=>void)|undefined
    setEvents([]);setDetail(null);setCurrent(null);setJobs([]);setStream('Connecting')
    const load=async()=>{
      let cursor=0
      const collected:TraceEvent[]=[]
      while(!cancelled){
        const result=await graphql<{events:TraceEvent[]}>('query($id:ID!,$after:Int!){events(runId:$id,after:$after){sequence sourceId payload}}',{id:selected,after:cursor})
        if(!result.events.length)break
        collected.push(...result.events);cursor=result.events.at(-1)!.sequence
      }
      if(cancelled)return
      setEvents(collected)
      unsubscribe=subscribeEvents(selected,cursor,event=>setEvents(previous=>[...previous,event]),setStream)
    }
    load().catch(e=>{if(!cancelled)setError(e.message)})
    const state=async()=>{
      try{const result=await graphql<{run:Run;currentItem:CurrentItem|null;jobs:Job[]}>(DETAILS,{id:selected})
        if(!cancelled){setRun(result.run);setCurrent(result.currentItem);setJobs(result.jobs)}}catch(e){if(!cancelled)setError((e as Error).message)}
    }
    state();const timer=setInterval(state,1000)
    return ()=>{cancelled=true;unsubscribe?.();clearInterval(timer)}
  },[selected])
  const busy=sending||jobs.some(job=>['pending','running'].includes(job.status))
  const submit=async(kind:string,payload:Record<string,unknown>={})=>{
    setSending(true);setError('')
    try{const result=await graphql<{submitCommand:Job}>('mutation($id:ID!,$request:String!,$kind:String!,$payload:JSON!){submitCommand(runId:$id,requestId:$request,kind:$kind,payload:$payload){id kind status result}}',
      {id:selected,request:crypto.randomUUID(),kind,payload})
      setJobs(previous=>[result.submitCommand,...previous]);if(kind==='label'||kind==='skip')setCurrent(null)
    }catch(e){setError((e as Error).message)}finally{setSending(false)}
  }
  const create=async()=>{
    setSending(true);setError('')
    const selection_policy=objective==='recall'?{primary:'recall',secondary:'accuracy',positive_class:'include'}:{primary:objective,positive_class:'include'}
    try{const result=await graphql<{createRun:Run}>('mutation($name:String!,$config:JSON!,$confirmed:Boolean!){createRun(name:$name,mode:"live",config:$config,confirmed:$confirmed){id name mode status createdAt config}}',
      {name,confirmed,config:{selection_policy,max_requests:maxRequests,max_optimizer_calls:maxOptimizer}})
      await refresh();setSelected(result.createRun.id);setCreating(false);setView('label')
    }catch(e){setError((e as Error).message)}finally{setSending(false)}
  }
  const recent=events.filter(event=>['optimizer-request','optimizer-response','decision-request','decision-response','fit-completed','candidate-evaluated','optimization-stage-completed'].includes(String(event.payload.kind))).slice(-8).reverse()
  const metrics=events.findLast(event=>event.payload.kind==='cycle-metrics')?.payload.metrics as {accuracy:number;per_class?:Record<string,{precision:number|null;recall:number|null}>}|undefined
  const percent=(value:number|null|undefined)=>value==null?'—':`${(value*100).toFixed(1)}%`
  return <div className="app-shell bg-background text-foreground">
    <header className="flex items-center justify-between border-b border-border px-5 py-3"><div className="flex items-center gap-3"><span className="flex size-9 items-center justify-center rounded-xl bg-primary text-primary-foreground"><RefreshCw className="size-5" /></span><div><p className="font-semibold">Decision Flywheel</p><p className="text-xs text-muted-foreground">Persistent experiment workspace</p></div></div><Badge variant="outline">Local · GraphQL · SQLite</Badge></header>
    <div className="flex min-h-0 flex-1">
      <aside className="flex w-60 shrink-0 flex-col gap-3 overflow-y-auto border-r border-border bg-muted/20 p-4"><div className="flex items-center justify-between"><p className="flex items-center gap-2 text-sm font-semibold"><History className="size-4" />Run history</p><Button variant="ghost" size="icon" aria-label="Refresh run history" onClick={()=>refresh().catch(e=>setError(e.message))}><RefreshCw /></Button></div><Button variant="outline" disabled={!capabilities.liveEnabled} onClick={()=>setCreating(!creating)}><Plus />New run</Button>
        {runs.map(item=><button key={item.id} onClick={()=>{setSelected(item.id);setRevision(0)}} className={`rounded-lg border p-3 text-left ${selected===item.id?'border-primary bg-accent':'border-transparent hover:bg-accent/50'}`}><p className="text-sm font-medium">{item.name}</p><p className="mt-1 text-xs text-muted-foreground">{item.config.selection_policy?.primary??'Recorded'}{item.config.selection_policy?.secondary?` / ${item.config.selection_policy.secondary}`:''}</p><p className="mt-1 text-xs text-muted-foreground">{new Date(item.createdAt).toLocaleDateString()} · {item.mode}</p></button>)}
        {!runs.length?<p className="text-xs leading-relaxed text-muted-foreground">No runs yet. Import a recording through the API or create a live run.</p>:null}
      </aside>
      <main className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 p-4">
        {error?<div role="alert" className="rounded-lg border border-destructive/40 bg-destructive/5 p-3 text-sm">{error}</div>:null}
        {error==='Authentication required'?<form className="flex gap-2" onSubmit={async event=>{event.preventDefault();const response=await fetch('/session',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token})});setToken('');if(response.ok){setError('');refresh()}else setError('Authentication required')}}><Input type="password" aria-label="Workspace access token" value={token} onChange={event=>setToken(event.target.value)} /><Button type="submit">Connect</Button></form>:null}
        {creating?<Card className="gap-2"><CardHeader><CardTitle className="text-base">Start an independent run</CardTitle></CardHeader><CardContent className="flex flex-wrap items-end gap-3"><div className="space-y-1"><Label htmlFor="run-name">Name</Label><Input id="run-name" value={name} onChange={event=>setName(event.target.value)} placeholder="Recall / accuracy experiment" /></div><div className="space-y-1"><Label htmlFor="run-objective">Objective</Label><NativeSelect id="run-objective" value={objective} onChange={event=>setObjective(event.target.value)}><NativeSelectOption value="f1">F1 (Include)</NativeSelectOption><NativeSelectOption value="recall">Recall / accuracy guard</NativeSelectOption><NativeSelectOption value="accuracy">Accuracy</NativeSelectOption></NativeSelect></div><div className="w-28 space-y-1"><Label htmlFor="max-requests">Request limit</Label><Input id="max-requests" type="number" min={1} value={maxRequests} onChange={event=>setMaxRequests(Number(event.target.value))} /></div><div className="w-28 space-y-1"><Label htmlFor="max-optimizer">Optimizer limit</Label><Input id="max-optimizer" type="number" min={1} value={maxOptimizer} onChange={event=>setMaxOptimizer(Number(event.target.value))} /></div><Label className="flex items-center gap-2"><input type="checkbox" checked={confirmed} onChange={event=>setConfirmed(event.target.checked)} />Authorize paid calls within these limits</Label><Button disabled={sending||!confirmed||!name.trim()||capabilities.itemCount===0} onClick={create}>Create run</Button></CardContent></Card>:null}
        {run?<><div className="flex flex-wrap items-center justify-between gap-3"><div><h1 className="text-xl font-semibold">{run.name}</h1><p className="mt-1 text-xs text-muted-foreground">{events.length} committed events · {run.config.selection_policy?.primary??'Recorded objective'} · <span role="status">{busy?'Working':run.status}</span> · stream {stream}</p></div><div className="flex gap-2"><Button variant={view==='timeline'?'secondary':'ghost'} onClick={()=>setView('timeline')}>Timeline</Button><Button variant={view==='label'?'secondary':'ghost'} disabled={run.mode!=='live'} onClick={()=>setView('label')}>Label items</Button></div></div>
          {view==='timeline'?<div className="flex min-h-0 flex-1 flex-col gap-2"><div className="flex items-center justify-between"><p className="text-xs text-muted-foreground">Click a cycle or event to inspect the stored request, response, and configuration.</p><Button variant="outline" size="sm" onClick={()=>setRevision(events.at(-1)?.sequence??0)}><RefreshCw />Load latest events</Button></div><iframe title="Run timeline and event inspector" src={`/runs/${selected}/timeline?revision=${revision}`} className="min-h-0 w-full flex-1 rounded-xl border border-border" /></div>:
          <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 overflow-y-auto lg:grid-cols-[minmax(0,1fr)_minmax(300px,420px)]"><section className="space-y-3"><div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={()=>submit('prepare')}>{current?'Refresh prediction':'Prepare next item'}</Button><Button variant="outline" disabled={busy} onClick={()=>submit('undo')}><Undo2 />Undo last review</Button></div>{current?<LabelCard key={current.prediction.presentation_id} current={current} busy={busy} onSubmit={submit} />:<Card><CardContent><p className="text-sm text-muted-foreground">{busy?'Processing this cycle. Events stream on the right.':'Prepare an item to get its prediction before voting.'}</p></CardContent></Card>}<p className="text-xs text-muted-foreground">Historical reviewed agreement: accuracy {percent(metrics?.accuracy)} · Include precision {percent(metrics?.per_class?.include?.precision)} · recall {percent(metrics?.per_class?.include?.recall)}. This is not a protected final-model evaluation.</p>{jobs[0]?.status==='failed'||jobs[0]?.status==='interrupted'?<p role="alert" className="text-sm text-destructive">Last command {jobs[0].status}. State retained; no automatic paid retry.</p>:null}</section><aside className="space-y-3"><div className="flex items-center gap-2 text-sm font-semibold"><Activity className="size-4" />Live engine activity</div>{recent.map(event=><button key={event.sequence} onClick={()=>setDetail(event)} className="flex w-full items-center justify-between rounded-lg border border-border bg-card px-3 py-2 text-left text-xs"><span className="capitalize">{String(event.payload.kind).replaceAll('-',' ')}</span><span className="font-mono text-muted-foreground">{String(event.payload.cycle_number??'')}</span></button>)}{detail?<TraceDetail event={detail} />:<p className="text-xs text-muted-foreground">Select a streamed model request or response to inspect its exact content.</p>}</aside></div>}
        </>:<div className="flex flex-1 items-center justify-center"><div className="max-w-sm text-center"><RefreshCw className="mx-auto mb-4 size-8 text-muted-foreground" /><h1 className="text-xl font-semibold">A history you can inspect</h1><p className="mt-2 text-sm leading-relaxed text-muted-foreground">Choose a recorded run, or start a new labeling session. Every optimization exchange is stored by the API.</p></div></div>}
      </main>
    </div>
  </div>
}
