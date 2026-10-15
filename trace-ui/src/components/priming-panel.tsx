import {useEffect,useRef,useState,type ReactNode} from 'react'
import {Button} from './ui/button'
import {Input} from './ui/input'
import {Label} from './ui/label'
import {LabelingReview} from './labeling-review'

export type ClassifierRef={id:string;revision:number}
export type PrimingField={name:string;label:string;required?:boolean;multiline?:boolean}
export type PrimingContract={list_id:string;schema:Record<string,unknown>;classifiers:Array<ClassifierRef&{name:string;config:{question:string;classes:Array<{label:string}>}}>;transformers:Array<{id:string;name:string;fields:PrimingField[]}>}
export type PrimingLabels=Record<string,{label:string;rationale?:string}>
export type PrimingDraft={id:string;version:number;status:string;contract:PrimingContract;source:{kind:string};rows:Array<{row:number;item:{id:string;values:Record<string,unknown>};labels:PrimingLabels;model_representation:Record<string,unknown>}>;errors:Array<{row:number;field:string;message:string}>;fields:PrimingField[];message?:string|null;remote_cancel?:string}
export type PrimingClient={
 contract:(listId:string,classifiers:ClassifierRef[])=>Promise<PrimingContract>
 template:(listId:string,classifiers:ClassifierRef[],format:'csv'|'xlsx')=>Promise<{content:string;encoding:string}>
 get:(identifier:string)=>Promise<PrimingDraft>
 command:(action:string,payload:Record<string,unknown>)=>Promise<PrimingDraft>
}
export type PrimingPanelProps={client:PrimingClient;listId:string;classifiers:ClassifierRef[];initialDraftId?:string;pollInterval?:number;onDraftChange?:(draft:PrimingDraft)=>void;onAccepted?:(draft:PrimingDraft)=>void;renderItem?:(item:PrimingDraft["rows"][number]["item"])=>ReactNode}

function InputFields({fields,values,onChange,disabled}:{fields:PrimingField[];values:Record<string,string>;onChange:(key:string,value:string)=>void;disabled:boolean}){
 return <div className="grid gap-3">{fields.map(field=><label key={field.name} className="grid gap-1 text-sm"><span>{field.label}{field.required?' *':''}</span>{field.multiline?<textarea aria-label={field.label} disabled={disabled} required={field.required} value={values[field.name]??''} onChange={event=>onChange(field.name,event.target.value)} className="min-h-24 rounded-md border border-input bg-background p-2"/>:<Input aria-label={field.label} disabled={disabled} required={field.required} value={values[field.name]??''} onChange={event=>onChange(field.name,event.target.value)}/>}</label>)}</div>
}

function download(content:string,encoding:string,name:string){
 const bytes=encoding==='base64'?Uint8Array.from(atob(content),c=>c.charCodeAt(0)):content
 const url=URL.createObjectURL(new Blob([bytes]));const anchor=document.createElement('a');anchor.href=url;anchor.download=name;anchor.click();URL.revokeObjectURL(url)
}

/** Host supplies transport; Papyrus and standalone Cyclotron share this workflow. */
export function PrimingPanel({client,listId,classifiers,initialDraftId,pollInterval=2000,onDraftChange,onAccepted,renderItem}:PrimingPanelProps){
 const [contract,setContract]=useState<PrimingContract|null>(null),[draft,setDraft]=useState<PrimingDraft|null>(null)
 const [route,setRoute]=useState<'file'|'transform'>('file'),[transformer,setTransformer]=useState(''),[inputs,setInputs]=useState<Record<string,string>>({}),[answers,setAnswers]=useState<Record<string,string>>({})
 const [feedback,setFeedback]=useState(''),[labels,setLabels]=useState<PrimingLabels[]>([]),[busy,setBusy]=useState(false),[error,setError]=useState(''),[starting,setStarting]=useState(false)
 const epoch=useRef(0),cancelled=useRef(false),mounted=useRef(true),draftCallback=useRef(onDraftChange),acceptedCallback=useRef(onAccepted),request=useRef<{action:string;payload:Record<string,unknown>}|null>(null)
 draftCallback.current=onDraftChange;acceptedCallback.current=onAccepted
 const pins=JSON.stringify(classifiers)
 const apply=(value:PrimingDraft)=>{
  if(!mounted.current)return
  setDraft(value);setAnswers({});draftCallback.current?.(value)
  if(value.status==='preview'){setLabels(value.rows.map(row=>structuredClone(row.labels)));setFeedback('')}
  if(value.status==='accepted')acceptedCallback.current?.(value)
 }
 useEffect(()=>{
  mounted.current=true;epoch.current+=1;let live=true
  setContract(null);setDraft(null);setError('');setBusy(false);setStarting(false);setLabels([]);setInputs({});request.current=null;cancelled.current=false
  Promise.all([client.contract(listId,JSON.parse(pins)),initialDraftId?client.get(initialDraftId):Promise.resolve(null)]).then(([value,existing])=>{
   if(!live)return;setContract(value);setTransformer(value.transformers[0]?.id??'');if(existing)apply(existing)
  }).catch(reason=>{if(live)setError((reason as Error).message)})
  return()=>{live=false;mounted.current=false}
 },[client,listId,pins,initialDraftId])
 useEffect(()=>{
  if(!draft||!pollInterval||!['running','queued','starting'].includes(draft.status))return
  let live=true;const timer=setTimeout(()=>{
   client.command('poll',{identifier:draft.id}).then(value=>{if(live&&!cancelled.current)apply(value)}).catch(reason=>{if(live)setError((reason as Error).message)})
  },pollInterval)
  return()=>{live=false;clearTimeout(timer)}
 },[client,draft,pollInterval])
 const execute=async(action:string,payload:Record<string,unknown>)=>{
  const generation=epoch.current
  request.current={action,payload};setBusy(true);setError('')
  try{
   const value=await client.command(action,payload)
   if(!mounted.current||generation!==epoch.current){if(!['accepted','cancelled'].includes(value.status))await client.command('cancel',{identifier:value.id});return}
   if(cancelled.current&&action!=='cancel'&&!['accepted','cancelled'].includes(value.status))apply(await client.command('cancel',{identifier:value.id}))
   else apply(value)
  }catch(reason){if(mounted.current&&generation===epoch.current)setError((reason as Error).message)}finally{if(mounted.current&&generation===epoch.current){setBusy(false);setStarting(false)}}
 }
 const start=(action:string,payload:Record<string,unknown>)=>{
  cancelled.current=false;setStarting(true);setDraft(null);void execute(action,{identifier:crypto.randomUUID(),list_id:listId,refs:JSON.parse(pins),...payload})
 }
 const cancel=()=>{
  cancelled.current=true
  if(draft)void execute('cancel',{identifier:draft.id})
 }
 const upload=async(file:File)=>{
  if(file.size>4_000_000){setError('File exceeds the 4 MB limit');return}
  const format=file.name.split('.').pop()?.toLowerCase()
  if(!['csv','json','jsonl','xlsx'].includes(format??'')){setError('Choose CSV, JSON, JSONL or Excel (.xlsx)');return}
  try{const bytes=new Uint8Array(await file.arrayBuffer());let binary='';for(const byte of bytes)binary+=String.fromCharCode(byte);start('file',{kind:format,content:btoa(binary)})}catch(reason){setError((reason as Error).message)}
 }
 const active=contract?.transformers.find(row=>row.id===transformer)
 const validLabels=draft?.rows.every((_row,index)=>contract?.classifiers.every(row=>labels[index]?.[row.id]?.label))??false
 const updateLabel=(index:number,cid:string,key:'label'|'rationale',value:string)=>setLabels(old=>old.map((row,n)=>n===index?{...row,[cid]:{label:row[cid]?.label??'',rationale:row[cid]?.rationale??'',[key]:value}}:row))
 return <section aria-label="Optional example priming" className="space-y-4 rounded-lg border border-border bg-background p-4" data-cyclotron-priming>
  <div><h2 className="text-lg font-semibold">Curate representative examples</h2><p className="text-sm text-muted-foreground">Optional. Review complete items and choose your own labels before saving. Explanations are optional.</p></div>
  {error?<p role="alert" className="text-sm text-destructive">{error}</p>:null}
  {!contract?<p role="status">Loading import configuration…</p>:<>
   {!draft&&!starting?<>
    <div className="flex gap-2"><Button variant={route==='file'?'secondary':'outline'} onClick={()=>setRoute('file')}>From file</Button>{contract.transformers.length?<Button variant={route==='transform'?'secondary':'outline'} onClick={()=>setRoute('transform')}>From reference / URL</Button>:null}</div>
    {route==='file'?<div className="space-y-3"><p className="text-sm">Excel, CSV, JSON or JSONL. Every selected output needs a label. Explanations are optional.</p><div className="flex gap-2">{(['csv','xlsx'] as const).map(format=><Button key={format} variant="outline" disabled={busy} onClick={()=>{setError('');client.template(listId,JSON.parse(pins),format).then(value=>download(value.content,value.encoding,`cyclotron-examples.${format}`)).catch(reason=>setError((reason as Error).message))}}>Download {format==='xlsx'?'Excel':'CSV'} template</Button>)}</div><Input aria-label="Example file" type="file" accept=".csv,.json,.jsonl,.xlsx" disabled={busy} onChange={event=>{const file=event.target.files?.[0];if(file)void upload(file)}}/></div>:<form className="space-y-3" onSubmit={event=>{event.preventDefault();start('transform',{transformer_id:transformer,inputs})}}><label className="grid gap-1 text-sm">Curation service<select aria-label="Curation service" className="rounded-md border p-2" value={transformer} onChange={event=>{setTransformer(event.target.value);setInputs({})}}>{contract.transformers.map(row=><option key={row.id} value={row.id}>{row.name}</option>)}</select></label><InputFields fields={active?.fields??[]} values={inputs} onChange={(key,value)=>setInputs(old=>({...old,[key]:value}))} disabled={busy}/><Button disabled={busy}>Start curation</Button><p className="text-xs text-muted-foreground">The configured service processes source content. A URL alone is not an example.</p></form>}
   </>:null}
   {starting?<p role="status">Starting import…</p>:null}
   {draft?.message?<p role="status">{draft.message}</p>:null}
   {draft?.errors.length?<ul role="alert" className="space-y-1 text-sm text-destructive">{draft.errors.map((row,index)=><li key={index}>Row {row.row} · {row.field}: {row.message}</li>)}</ul>:null}
   {draft&&['running','queued','starting'].includes(draft.status)?<p role="status">Curation in progress… <Button variant="outline" disabled={busy} onClick={()=>void execute('poll',{identifier:draft.id})}>Check progress</Button></p>:null}
   {draft?.status==='preview'?<><p className="text-sm font-medium">Preview · {draft.rows.length} example{draft.rows.length===1?'':'s'}. Nothing saved yet.</p>{draft.rows.map((row,index)=><article key={row.item.id} className="space-y-3 rounded-md border border-border p-3"><h3 className="break-words font-semibold">{String(row.item.values.title??row.item.id)}</h3>{renderItem?renderItem(row.item):<dl className="space-y-2">{Object.entries(row.item.values).map(([key,value])=><div key={key}><dt className="text-xs font-medium text-muted-foreground">{key}</dt><dd className="whitespace-pre-wrap text-sm">{typeof value==='string'?value:JSON.stringify(value)}</dd></div>)}</dl>}<details><summary className="text-sm font-medium">Exact item values supplied to each decision model</summary><pre className="max-h-60 overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 text-xs">{JSON.stringify(row.model_representation,null,2)}</pre></details>{contract.classifiers.map(classifier=><LabelingReview key={classifier.id} targetId={`priming-${row.item.id}-${classifier.id}`} classifierName={classifier.name} classes={classifier.config.classes.map(c=>c.label)} selectedLabel={labels[index]?.[classifier.id]?.label??''} explanationDraft={labels[index]?.[classifier.id]?.rationale??''} busy={busy} onSelectedLabelChange={value=>updateLabel(index,classifier.id,'label',value)} onExplanationDraftChange={value=>updateLabel(index,classifier.id,'rationale',value)}/>)}</article>)}<Button disabled={busy||!validLabels} onClick={()=>void execute('accept',{identifier:draft.id,version:draft.version,labels})}>Accept examples and labels</Button></>:null}
   {draft&&draft.source.kind==='transform'&&['preview','clarification','failed','invalid'].includes(draft.status)?<form className="space-y-3 rounded-md border border-border p-3" onSubmit={event=>{event.preventDefault();void execute('feedback',{identifier:draft.id,version:draft.version,request_id:crypto.randomUUID(),reason:feedback,inputs:answers})}}><Label htmlFor={`feedback-${draft.id}`}>Transformation feedback</Label><textarea id={`feedback-${draft.id}`} required className="min-h-20 w-full rounded-md border border-input bg-background p-2 text-sm" value={feedback} onChange={event=>setFeedback(event.target.value)} disabled={busy}/><p className="text-xs text-muted-foreground">Describe extraction or curation changes here. Your label explanation is recorded separately.</p><InputFields fields={draft.fields} values={answers} onChange={(key,value)=>setAnswers(old=>({...old,[key]:value}))} disabled={busy}/><Button variant="outline" disabled={busy||!feedback.trim()}>{draft.status==='clarification'?'Send clarification':'Reject preview and revise'}</Button></form>:null}
   {(draft&&!['accepted','cancelled'].includes(draft.status))||starting?<Button variant="ghost" onClick={cancel}>Cancel import</Button>:null}
   {draft?.status==='cancelled'?<p role="status">Import cancelled. No examples were saved.{draft.remote_cancel==='pending'?' Remote cancellation is pending; retry cancellation to confirm.':''}</p>:null}
   {draft?.remote_cancel==='pending'?<Button onClick={cancel} disabled={busy}>Retry cancellation</Button>:null}
   {draft?.status==='accepted'?<p role="status">Examples and human labels saved.</p>:null}
   {error&&request.current?<Button variant="outline" disabled={busy} onClick={()=>{const previous=request.current!;void execute(previous.action,previous.payload)}}>Retry last request</Button>:null}
   {draft&&['cancelled','accepted','invalid'].includes(draft.status)?<Button variant="outline" disabled={busy} onClick={()=>{setDraft(null);setError('');cancelled.current=false}}>Import another file or reference</Button>:null}
  </>}
 </section>
}
