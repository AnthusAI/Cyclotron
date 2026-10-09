import {useState} from 'react'
import {AppDrawer} from './AppDrawer'
import {graphql} from './graphql'
import {requestId} from './requestId'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import type {CatalogClassifier} from './CyclotronEditor'

export function NewClassifier({onSaved,onCancel}:{onSaved:(classifier:CatalogClassifier)=>void;onCancel:()=>void}){
  const [name,setName]=useState(''),[question,setQuestion]=useState(''),[classes,setClasses]=useState(''),[positive,setPositive]=useState('')
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const [identifier]=useState(requestId)
  const labels=classes.split('\n').map(row=>row.trim()).filter(Boolean)
  const valid=name.trim()&&question.trim()&&labels.length>1&&new Set(labels).size===labels.length&&(!positive||labels.includes(positive))
  const save=async()=>{
    setBusy(true);setError('')
    try{
      const result=await graphql<{saveClassifier:CatalogClassifier}>('mutation($id:String!,$name:String!,$config:JSON!){saveClassifier(identifier:$id,name:$name,config:$config)}',{id:identifier,name,config:{question,classes:labels.map(label=>({label,...(positive?{role:label===positive?'positive':'negative'}:{})}))}})
      onSaved(result.saveClassifier)
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <AppDrawer title="Create classifier" side="right" open onOpenChange={open=>{if(!open&&!busy)onCancel()}} footer={<><Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button><Button disabled={busy||!valid} onClick={save}>{busy?'Saving…':'Save classifier'}</Button></>}>
    <Label htmlFor="new-classifier-name">Classifier name</Label><Input id="new-classifier-name" disabled={busy} value={name} onChange={e=>setName(e.target.value)}/>
    <Label htmlFor="new-classifier-question">Decision question</Label><Input id="new-classifier-question" disabled={busy} value={question} onChange={e=>setQuestion(e.target.value)}/>
    <Label htmlFor="new-classifier-classes">Ordered classes (one per line)</Label><textarea id="new-classifier-classes" className="min-h-28 w-full rounded-md border bg-background p-3" disabled={busy} value={classes} onChange={e=>setClasses(e.target.value)}/>
    <Label htmlFor="new-positive-class">Positive class</Label><NativeSelect id="new-positive-class" disabled={busy} value={positive} onChange={e=>setPositive(e.target.value)}><NativeSelectOption value="">No designated positive class</NativeSelectOption>{[...new Set(labels)].map(label=><NativeSelectOption key={label} value={label}>{label}</NativeSelectOption>)}</NativeSelect>
    <p className="text-xs text-muted-foreground">The new classifier is added to this cyclotron draft. Save the cyclotron to publish its ordered membership. Existing runs remain unchanged.</p>
    {error?<p role="alert" className="text-sm text-destructive">{error}</p>:null}
  </AppDrawer>
}
