import {useEffect,useState} from 'react'
import {Pencil,Undo2} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {AppDrawer} from './AppDrawer'
import type {TraceEvent} from './graphql'
import type {MetricClassifier} from './ClassifierMetrics'
import {useCorrectionDraft,type CorrectionVote as Vote,type CorrectionEdit as Edit} from './correctionDraft'

export type FeedbackJob={id:string;kind:string;status:string;result:Record<string,unknown>|null}
const emptyEdit:Edit={rows:[],draft:{}}
function discardCompleted(edits:Record<string,Edit>,job:FeedbackJob){
  const item=job.result?.corrected??job.result?.undone
  if(typeof item!=='string')return edits
  const remaining={...edits};delete remaining[item];return remaining
}

function currentVotes(events:TraceEvent[]){
  const votes=new Map<string,Vote>()
  for(const {payload} of events){
    if(payload.kind!=='human-feedback')continue
    const f=payload.feedback as Record<string,unknown>|undefined
    if(!f||typeof f.id!=='string'||typeof f.item_id!=='string')continue
    const classifier=String(payload.classifier_id??f.score_name),key=JSON.stringify([classifier,f.item_id])
    votes.delete(key)
    if(payload.action==='submitted'&&typeof f.final_answer_value==='string')votes.set(key,{id:f.id,item:f.item_id,classifier,
      label:f.final_answer_value,comment:String(f.edit_comment_value??''),readonly:String(f.review_provenance??'').startsWith('replayed-human-vote:')})
  }
  return [...votes.values()]
}

export function FeedbackControls({runId,classifiers,events,jobs,busy,replay=false,onSubmit,onResume}:{
  runId:string;classifiers:MetricClassifier[];events:TraceEvent[];jobs:FeedbackJob[];busy:boolean;replay?:boolean;
  onSubmit:(kind:string,payload:Record<string,unknown>)=>Promise<FeedbackJob|null>;
  onResume:(id:string)=>Promise<FeedbackJob|null>;
}){
  const [mode,setMode]=useState<'edit'|'undo'|null>(null)
  const [{item,edits},setCorrection]=useCorrectionDraft(runId)
  const setItem=(item:string)=>setCorrection(previous=>({...previous,item}))
  const setEdits=(update:(previous:Record<string,Edit>)=>Record<string,Edit>)=>setCorrection(previous=>({...previous,edits:update(previous.edits)}))
  const {rows,draft}=edits[item]??emptyEdit
  const setDraft=(update:(previous:Edit['draft'])=>Edit['draft'])=>setEdits(previous=>{
    const edit=previous[item]
    return edit?{...previous,[item]:{...edit,draft:update(edit.draft)}}:previous
  })
  const [waiting,setWaiting]=useState<string|null>(null),[sending,setSending]=useState(false),[message,setMessage]=useState('')
  const votes=currentVotes(events),local=votes.filter(v=>!v.readonly)
  const items=[...new Set(local.map(v=>v.item))].reverse().slice(0,200)
  const locked=busy||sending||Boolean(waiting)
  const content=new Map<string,Record<string,unknown>>()
  for(const {payload} of events){const record=payload.item as {id?:string;values?:Record<string,unknown>}|undefined
    if(record?.id&&record.values)content.set(record.id,record.values)}
  const failed=jobs.find(j=>['correct','undo'].includes(j.kind)&&['failed','interrupted'].includes(j.status))
  useEffect(()=>{
    if(!waiting)return
    const job=jobs.find(j=>j.id===waiting)
    if(job?.status==='completed'){setWaiting(null);setMode(null);setCorrection(previous=>({item:'',edits:discardCompleted(previous.edits,job)}));setMessage('Feedback updated. Metrics use the saved original prediction.')}
    else if(job&&['failed','interrupted'].includes(job.status)){setWaiting(null);setMessage('Feedback needs attention. Recover the original command before trying another edit.')}
  },[jobs,waiting,setCorrection])
  if(replay)return null
  const choose=(id:string,discard=false)=>{
    const selected=votes.filter(v=>v.item===id)
    setItem(id)
    // Keep the vote identity captured when editing began; newer streamed votes
    // must not silently authorize an old draft against different feedback.
    setEdits(previous=>previous[id]&&!discard?previous:{...previous,[id]:{rows:selected,draft:Object.fromEntries(selected.map(v=>[v.classifier,{label:v.label,comment:v.comment}]))}})
  }
  const perform=async(action:()=>Promise<FeedbackJob|null>)=>{
    setSending(true);setMessage('Recording feedback changes…')
    try{
      const job=await action()
      if(!job){setMessage('The command could not be confirmed. Your edit is retained.');return}
      if(job.status==='completed'){setMode(null);setEdits(previous=>discardCompleted(previous,job));setItem('');setMessage('Feedback updated. Metrics use the saved original prediction.')}
      else{setWaiting(job.id);setMessage('Feedback command queued. Waiting for the recorded result…')}
    }catch(error){setMessage((error as Error).message)}finally{setSending(false)}
  }
  const changes=rows.filter(v=>!v.readonly&&draft[v.classifier]&&(draft[v.classifier].label!==v.label||draft[v.classifier].comment!==v.comment))
  const saved=votes.filter(v=>v.item===item)
  const stale=rows.length>0&&(rows.length!==saved.length||rows.some(row=>!saved.some(v=>v.classifier===row.classifier&&v.id===row.id)))
  return <div className="space-y-2">
    <div className="flex flex-wrap gap-2"><Button variant="outline" disabled={locked||!local.length} onClick={()=>{choose(items.includes(item)?item:items[0]);setMode('edit')}}><Pencil/>Edit recorded feedback</Button>
      <Button variant="outline" disabled={locked||!local.length} onClick={()=>setMode('undo')}><Undo2/>Undo item labels</Button>
      {failed?<Button variant="outline" disabled={locked} onClick={()=>void perform(()=>onResume(failed.id))}>Recover feedback command</Button>:null}</div>
    {message?<p role="status" className="text-xs text-muted-foreground">{message}</p>:null}
    <AppDrawer title={mode==='undo'?'Undo item labels':'Edit recorded feedback'} side="right" open={mode!==null} onOpenChange={open=>{if(!open&&!locked)setMode(null)}} footer={mode==='undo'?
      <Button disabled={locked} onClick={()=>void perform(()=>onSubmit('undo',{}))}>Retract labels and review again</Button>:
      <Button disabled={locked||stale||!changes.length} onClick={()=>void perform(()=>onSubmit('correct',{item_id:item,labels:changes.map(v=>({classifier_id:v.classifier,label:draft[v.classifier].label,comment:draft[v.classifier].comment,expected_feedback_id:v.id}))}))}>Save correction</Button>}>
      {mode==='undo'?<p className="text-sm leading-relaxed">Retract local labels for the most recently reviewed item and review its saved prediction again. Original labels stay in history. Inherited source labels stay unchanged. No model calls are made.</p>:<>
        <Label htmlFor="correction-item">Reviewed item</Label><NativeSelect id="correction-item" disabled={locked} value={item} onChange={e=>choose(e.target.value)}>{items.map(id=><NativeSelectOption key={id} value={id}>{String(content.get(id)?.title??id)}</NativeSelectOption>)}</NativeSelect>
        <div className="space-y-2"><h3 className="text-base font-semibold">{String(content.get(item)?.title??item)}</h3><p className="whitespace-pre-wrap text-sm leading-relaxed">{String(content.get(item)?.abstract??content.get(item)?.text??'Item text was not recorded in this history.')}</p></div>
        <p className="text-xs text-muted-foreground">Changes update metrics and invalidate dependent learning. Original predictions and feedback remain in history. No model calls are made.</p>
        {stale?<p role="alert" className="text-sm text-destructive">Saved feedback changed since this draft began. Your draft is retained; review the saved feedback before submitting a new correction.</p>:null}
        {stale||changes.length?<Button variant="outline" disabled={locked} onClick={()=>choose(item,true)}>Discard draft and load saved feedback</Button>:null}
        {rows.map(v=>{const name=classifiers.find(c=>c.id===v.classifier)?.name??v.classifier;const classes=classifiers.find(c=>c.id===v.classifier)?.config?.classes??[{label:v.label}];return <fieldset key={v.classifier} disabled={locked||v.readonly} className="space-y-3 rounded-lg border border-border p-3"><legend className="px-1 text-sm font-semibold">{name}</legend>
          <Label htmlFor={`correction-label-${v.classifier}`}>Label for {name}</Label><NativeSelect id={`correction-label-${v.classifier}`} value={draft[v.classifier]?.label??v.label} onChange={e=>setDraft(p=>({...p,[v.classifier]:{...p[v.classifier],label:e.target.value}}))}>{classes.map(c=><NativeSelectOption key={c.label} value={c.label}>{c.label}</NativeSelectOption>)}</NativeSelect>
          <Label htmlFor={`correction-comment-${v.classifier}`}>Explanation for {name}</Label><textarea id={`correction-comment-${v.classifier}`} rows={4} className="w-full rounded-md border border-input bg-background p-3 text-sm" value={draft[v.classifier]?.comment??v.comment} onChange={e=>setDraft(p=>({...p,[v.classifier]:{...p[v.classifier],comment:e.target.value}}))}/>
          {v.readonly?<p className="text-xs text-muted-foreground">Inherited source feedback is read-only.</p>:null}
        </fieldset>})}
      </>}
    </AppDrawer>
  </div>
}
