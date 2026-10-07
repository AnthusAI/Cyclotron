import {useEffect,useState} from 'react'
import {AppDrawer} from './AppDrawer'
import {graphql} from './graphql'
import {Button} from '@/components/ui/button'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'

export type ClassifierDefinition={id:string;name:string;revision:number;created_at?:string;fingerprint?:string;config:{question:string;classes:{label:string;role?:string}[];[key:string]:unknown}}
export function ClassifierHistory({classifier,onClose,onEdit}:{classifier:ClassifierDefinition;onClose:()=>void;onEdit?:(value:ClassifierDefinition)=>void}){
  const [versions,setVersions]=useState<ClassifierDefinition[]>([]),[revision,setRevision]=useState(classifier.revision),[error,setError]=useState('')
  useEffect(()=>{
    let cancelled=false
    graphql<{classifierVersions:ClassifierDefinition[]}>('query($id:ID!){classifierVersions(classifierId:$id)}',{id:classifier.id}).then(result=>{if(!cancelled)setVersions(result.classifierVersions)}).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[classifier.id])
  const inspected=versions.find(row=>row.revision===revision)
  return <AppDrawer title="Classifier history" side="right" open onOpenChange={open=>{if(!open)onClose()}} footer={onEdit?<Button variant="outline" disabled={!inspected} onClick={()=>{if(inspected)onEdit(inspected)}}>Edit from this revision</Button>:undefined}>
    <p className="font-semibold">{classifier.name}</p>
    <Label htmlFor="classifier-history-revision">Classifier revision</Label><NativeSelect id="classifier-history-revision" value={revision} onChange={e=>setRevision(Number(e.target.value))}>{versions.map(row=><NativeSelectOption key={row.revision} value={row.revision}>Revision {row.revision}{row.revision===classifier.revision?' · pinned here':''}</NativeSelectOption>)}</NativeSelect>
    {error?<p role="alert" className="text-sm text-destructive">{error}</p>:inspected?<>
      <h3 className="font-semibold">{inspected.name}</h3><p className="text-sm whitespace-pre-wrap">{inspected.config.question}</p>
      <ol className="space-y-2">{inspected.config.classes.map(row=><li key={row.label} className="rounded-md border p-2 text-sm">{row.label}{row.role?` · ${row.role}`:''}</li>)}</ol>
      <p className="text-xs text-muted-foreground">Read-only revision. Editing from it opens a draft; saving creates a new revision and updates active scorecard definitions. Existing runs keep their pinned configuration.</p>
      <details className="disclosure"><summary>Complete definition</summary><pre>{JSON.stringify(inspected,null,2)}</pre></details>
    </>:<p role="status" className="text-sm text-muted-foreground">Loading classifier history…</p>}
  </AppDrawer>
}
