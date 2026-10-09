import {useEffect,useState} from 'react'
import {AppDrawer} from './AppDrawer'
import {graphql} from './graphql'
import type {ClassifierDefinition} from './ClassifierHistory'
import type {Definition} from './CyclotronEditor'

type Position={position:number;revision:number}
type Comparison={before:Definition;after:Definition;changes:{name:{before:string;after:string}|null;
  members:{id:string;before:Position|null;after:Position|null}[];
  settings:{key:string;before_present:boolean;before:unknown;after_present:boolean;after:unknown}[]};
  classifiers:{id:string;before:ClassifierDefinition|null;after:ClassifierDefinition|null}[]}
const settingValue=(present:boolean,value:unknown)=>!present?'Not set':typeof value==='string'?value:JSON.stringify(value,null,2)

function MemberVersion({label,definition,position}:{label:string;definition:ClassifierDefinition|null;position:Position|null}){
  return <section className="min-w-0 space-y-2 rounded-md border p-3">
    <h4 className="text-xs font-semibold text-muted-foreground">{label}</h4>
    {definition?<><p className="text-sm font-semibold">{definition.name}</p><p className="text-xs text-muted-foreground">Position {position?.position} · classifier revision {definition.revision}</p>
      <p className="whitespace-pre-wrap text-sm">{definition.config.question}</p>
      <ol className="space-y-1 text-xs">{definition.config.classes.map(row=><li key={row.label}>{row.label}{row.role?` · ${row.role}`:''}</li>)}</ol>
      <details><summary className="text-xs">Complete classifier configuration</summary><pre className="overflow-auto whitespace-pre-wrap break-words text-xs">{JSON.stringify(definition.config,null,2)}</pre></details>
    </>:<p className="text-sm text-muted-foreground">Not a member</p>}
  </section>
}

export function CyclotronComparison({cyclotronId,beforeRevision,afterRevision,onClose}:{cyclotronId:string;beforeRevision:number;afterRevision:number;onClose:()=>void}){
  const [value,setValue]=useState<Comparison|null>(null),[error,setError]=useState('')
  useEffect(()=>{
    let cancelled=false;setValue(null);setError('')
    graphql<{cyclotronDefinitionComparison:Comparison}>('query($id:ID!,$before:Int!,$after:Int!){cyclotronDefinitionComparison(cyclotronId:$id,beforeRevision:$before,afterRevision:$after)}',{id:cyclotronId,before:beforeRevision,after:afterRevision}).then(result=>{if(!cancelled)setValue(result.cyclotronDefinitionComparison)}).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[cyclotronId,beforeRevision,afterRevision])
  return <AppDrawer title="Compare cyclotron definitions" side="right" open onOpenChange={open=>{if(!open)onClose()}}>
    <p className="font-semibold">Active revision {beforeRevision} → inspected revision {afterRevision}</p>
    <p className="text-xs text-muted-foreground">Definition changes, not a performance comparison. Inspecting makes no model calls and changes no active definitions or runs.</p>
    {error?<p role="alert">{error}</p>:!value?<p role="status">Loading definition comparison…</p>:<>
      {value.changes.name?<p className="text-sm">Name: {value.changes.name.before} → {value.changes.name.after}</p>:null}
      <h3 className="font-semibold">Classifier membership and configuration</h3>
      {!value.changes.members.length?<p className="text-sm text-muted-foreground">No membership, order, or classifier-revision changes.</p>:value.changes.members.map(change=>{
        const definitions=value.classifiers.find(row=>row.id===change.id)
        return <section key={change.id} className="space-y-2"><p className="text-xs text-muted-foreground">{change.id}</p><div className="grid gap-2"><MemberVersion label="Active" definition={definitions?.before??null} position={change.before}/><MemberVersion label="Inspected" definition={definitions?.after??null} position={change.after}/></div></section>
      })}
      <h3 className="font-semibold">Shared settings</h3>
      <p className="text-xs text-muted-foreground">Shared request changes can affect every classifier in the cyclotron.</p>
      {!value.changes.settings.length?<p className="text-sm text-muted-foreground">No shared-setting changes.</p>:value.changes.settings.map(change=><section key={change.key} className="space-y-2 rounded-md border p-3"><h4 className="text-sm font-semibold">{change.key}</h4><p className="text-xs text-muted-foreground">Active</p><pre className="whitespace-pre-wrap break-words text-sm">{settingValue(change.before_present,change.before)}</pre><p className="text-xs text-muted-foreground">Inspected</p><pre className="whitespace-pre-wrap break-words text-sm">{settingValue(change.after_present,change.after)}</pre></section>)}
    </>}
  </AppDrawer>
}
