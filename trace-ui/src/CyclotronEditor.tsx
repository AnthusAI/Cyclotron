import {useState} from 'react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {Card,CardHeader,CardTitle,CardContent,CardFooter} from '@/components/ui/card'
import {graphql} from './graphql'
import {requestId} from './requestId'
import {NewClassifier} from './NewClassifier'
import {CyclotronSettings,validCyclotronSettings} from './CyclotronSettings'

export type Definition={id:string;name:string;revision:number;classifiers:{id:string;revision:number}[];settings?:Record<string,unknown>}
export type CatalogClassifier={id:string;name:string;revision:number}
export function CyclotronEditor({definition,classifiers,onSaved,onCancel}:{definition?:Definition;classifiers:CatalogClassifier[];onSaved:(value:Definition)=>void;onCancel:()=>void}){
  const [name,setName]=useState(definition?.name??'')
  const [identifier]=useState(()=>definition?.id??requestId())
  const [members,setMembers]=useState(definition?.classifiers??[])
  const [settings,setSettings]=useState(definition?.settings??{})
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const [creating,setCreating]=useState(false),[created,setCreated]=useState<CatalogClassifier[]>([])
  const available=[...classifiers,...created]
  const move=(index:number,offset:number)=>setMembers(previous=>{const next=[...previous];[next[index],next[index+offset]]=[next[index+offset],next[index]];return next})
  const save=async()=>{
    setBusy(true);setError('')
    try{
      const result=await graphql<{saveCyclotronDefinition:Definition}>('mutation($id:String!,$name:String!,$classifiers:JSON!,$settings:JSON!){saveCyclotronDefinition(identifier:$id,name:$name,classifiers:$classifiers,settings:$settings)}',{id:identifier,name,classifiers:members,settings})
      onSaved(result.saveCyclotronDefinition)
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <Card className="mx-5 flex min-h-0 flex-1 flex-col overflow-hidden"><CardHeader className="shrink-0"><CardTitle>{definition?'New cyclotron revision':'New cyclotron'}</CardTitle></CardHeader>
    <CardContent className="min-h-0 flex-1 space-y-4 overflow-y-auto">
      <Label htmlFor="cyclotron-name">Cyclotron name</Label><Input id="cyclotron-name" value={name} onChange={e=>setName(e.target.value)} disabled={busy}/>
      <p className="text-xs text-muted-foreground">Ordered membership. Saving does not start a run or modify existing sessions.</p>
      <CyclotronSettings settings={settings} onChange={setSettings} disabled={busy}/>
      <ol className="space-y-2">{members.map((ref,index)=><li key={ref.id} className="flex flex-wrap items-center gap-2 rounded-md border p-2"><span className="mr-auto text-sm">{available.find(row=>row.id===ref.id)?.name??ref.id} · revision {ref.revision}</span><Button variant="outline" disabled={busy||index===0} aria-label={`Move ${ref.id} earlier`} onClick={()=>move(index,-1)}>↑</Button><Button variant="outline" disabled={busy||index===members.length-1} aria-label={`Move ${ref.id} later`} onClick={()=>move(index,1)}>↓</Button><Button variant="ghost" disabled={busy} aria-label={`Remove ${ref.id}`} onClick={()=>setMembers(rows=>rows.filter(row=>row.id!==ref.id))}>Remove</Button></li>)}</ol>
      <div className="flex flex-wrap gap-2">{available.filter(row=>!members.some(ref=>ref.id===row.id)).map(row=><Button key={row.id} variant="outline" disabled={busy} onClick={()=>setMembers(rows=>[...rows,{id:row.id,revision:row.revision}])}>Add {row.name}</Button>)}<Button variant="outline" disabled={busy} onClick={()=>setCreating(true)}>Create classifier</Button></div>
      {creating?<NewClassifier onCancel={()=>setCreating(false)} onSaved={row=>{setCreated(rows=>[...rows,row]);setMembers(rows=>[...rows,{id:row.id,revision:row.revision}]);setCreating(false)}}/>:null}
      {error?<p role="alert" className="text-sm text-destructive">{error}</p>:null}
    </CardContent><CardFooter className="shrink-0 justify-end gap-2"><Button variant="ghost" disabled={busy} onClick={onCancel}>Cancel</Button><Button disabled={busy||!name.trim()||!members.length||!validCyclotronSettings(settings)} onClick={save}>{busy?'Saving…':'Save cyclotron'}</Button></CardFooter>
  </Card>
}
