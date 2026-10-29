import {useState} from 'react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {useWorkspaceTransport} from './workspaceTransportContext'

type Question={name:string;instructions:string;labels:string[]}
type Checkpoint={payload:{classifiers:Record<string,{state:{config:{tasks?:Question[]}}}>}}
type Choice={classifier:string;question:Question}
const SEPARATOR='\u0000'

/** Make one of this version's supporting questions a classifier of a new version, after an explicit confirmation. */
export function PromoteSupportingQuestion({runId,cyclotronName,nextRevision,onCreated,busy}:{
  runId:string;cyclotronName:string;nextRevision:number;onCreated:(runId:string)=>void;busy:boolean}){
  const {graphql}=useWorkspaceTransport()
  const [choices,setChoices]=useState<Choice[]|null>(null),[selected,setSelected]=useState('')
  const [classifierId,setClassifierId]=useState(''),[classifierName,setClassifierName]=useState(''),[positive,setPositive]=useState('')
  const [confirming,setConfirming]=useState(false),[saving,setSaving]=useState(false),[error,setError]=useState('')
  const load=async()=>{
    const data=await graphql<{cyclotronCheckpoints:Checkpoint[]}>('query($id:ID!){cyclotronCheckpoints(runId:$id)}',{id:runId})
    const latest=data.cyclotronCheckpoints.at(-1)
    setChoices(latest?Object.entries(latest.payload.classifiers).flatMap(([classifier,row])=>
      (row.state.config.tasks??[]).map(question=>({classifier,question}))):[])
  }
  const choice=choices?.find(row=>`${row.classifier}${SEPARATOR}${row.question.name}`===selected)
  const ready=Boolean(choice&&classifierId.trim()&&classifierName.trim())
  const create=async()=>{
    if(!choice)return
    setSaving(true);setError('')
    try{
      const result=await graphql<{promoteSupportingQuestion:{id:string}}>(
        'mutation($parentRunId:ID!,$sourceClassifierId:String!,$question:String!,$classifierId:String!,$classifierName:String!,$name:String!,$positiveLabel:String,$confirmed:Boolean!){promoteSupportingQuestion(parentRunId:$parentRunId,sourceClassifierId:$sourceClassifierId,question:$question,classifierId:$classifierId,classifierName:$classifierName,name:$name,positiveLabel:$positiveLabel,confirmed:$confirmed){id}}',
        {parentRunId:runId,sourceClassifierId:choice.classifier,question:choice.question.name,classifierId:classifierId.trim(),
         classifierName:classifierName.trim(),name:cyclotronName,positiveLabel:positive||null,confirmed:true})
      setConfirming(false)
      onCreated(result.promoteSupportingQuestion.id)
    }catch(e){setError((e as Error).message)}finally{setSaving(false)}
  }
  return <details className="text-xs" onToggle={event=>{if(event.currentTarget.open&&choices===null)void load().catch(e=>setError((e as Error).message))}}>
    <summary className="cursor-pointer text-muted-foreground">Make a supporting question a classifier</summary>
    <div className="space-y-2 py-2">
      {choices===null?null:choices.length===0?<p>No supporting questions are recorded for this version yet.</p>:<>
        <p className="text-muted-foreground">The supporting question becomes a classifier that reviewers label. Its instructions become the decision question and the first rubric; its labels become the classes.</p>
        <div className="grid gap-2 sm:grid-cols-2">
          <div className="space-y-1 sm:col-span-2"><Label htmlFor="promote-question">Supporting question</Label>
            <NativeSelect id="promote-question" aria-label="Supporting question" value={selected} disabled={busy||saving||confirming}
              onChange={event=>{setSelected(event.target.value);setPositive('')}}>
              <NativeSelectOption value="">Choose a supporting question</NativeSelectOption>
              {choices.map(row=><NativeSelectOption key={`${row.classifier}:${row.question.name}`} value={`${row.classifier}${SEPARATOR}${row.question.name}`}>{`${row.classifier} · ${row.question.name}: ${row.question.instructions}`}</NativeSelectOption>)}
            </NativeSelect></div>
          <div className="space-y-1"><Label htmlFor="promote-id">New classifier id</Label>
            <Input id="promote-id" value={classifierId} disabled={busy||saving||confirming} onChange={event=>setClassifierId(event.target.value)}/></div>
          <div className="space-y-1"><Label htmlFor="promote-name">New classifier name</Label>
            <Input id="promote-name" value={classifierName} disabled={busy||saving||confirming} onChange={event=>setClassifierName(event.target.value)}/></div>
          <div className="space-y-1"><Label htmlFor="promote-positive">Positive label</Label>
            <NativeSelect id="promote-positive" aria-label="Positive label" value={positive} disabled={busy||saving||confirming||!choice}
              onChange={event=>setPositive(event.target.value)}>
              <NativeSelectOption value="">No positive label</NativeSelectOption>
              {(choice?.question.labels??[]).map(label=><NativeSelectOption key={label} value={label}>{label}</NativeSelectOption>)}
            </NativeSelect></div>
        </div>
        {confirming&&choice?<div className="space-y-2 rounded-md border border-border p-2">
          <p role="status">This creates version {nextRevision} of {cyclotronName} with a new classifier, {classifierName.trim()}, from the supporting question {choice.question.name} of {choice.classifier}. The new classifier starts unlabeled. Earlier labels are replayed in the new version, so model calls follow when you prepare items there. This version stays as it is.</p>
          <div className="flex gap-2"><Button size="sm" disabled={busy||saving} onClick={()=>void create()}>{saving?'Creating…':'Create version'}</Button>
            <Button size="sm" variant="outline" disabled={saving} onClick={()=>setConfirming(false)}>Cancel</Button></div>
        </div>:<Button size="sm" variant="outline" disabled={busy||!ready} onClick={()=>{setError('');setConfirming(true)}}>Review</Button>}
      </>}
      {error?<p role="alert" className="text-destructive">{error}</p>:null}
    </div>
  </details>
}
