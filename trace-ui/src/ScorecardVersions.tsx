import {useEffect,useState} from 'react'
import {Button} from '@/components/ui/button'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {graphql} from './graphql'

type Scorecard={id:string;name:string;active_revision:number;versions:{revision:number;run_id:string}[]}
type Version={revision:number;run_id:string;metrics:Record<string,{count:number;accuracy:number|null;per_class:Record<string,{recall:number|null;precision:number|null}>}>;classifiers:{id:string;name:string;config:{classes:{label:string;role?:string}[]}}[]}
const percent=(value:number|null|undefined)=>value==null?'—':`${Math.round(value*100)}%`

export function ScorecardVersions({runId,onSelect,busy}:{runId:string;onSelect:(id:string)=>void;busy:boolean}){
  const [scorecard,setScorecard]=useState<Scorecard|null>(null),[versions,setVersions]=useState<Version[]>([]),[error,setError]=useState(''),[saving,setSaving]=useState(false)
  useEffect(()=>{
    let cancelled=false
    setScorecard(null);setVersions([])
    void (async()=>{
      const result=await graphql<{scorecards:Scorecard[]}>('{scorecards}')
      const found=result.scorecards.find(row=>row.versions.some(version=>version.run_id===runId))
      if(!found||cancelled)return
      const data=await graphql<{scorecardVersions:Version[]}>('query($id:ID!){scorecardVersions(scorecardId:$id)}',{id:found.id})
      if(!cancelled){setScorecard(found);setVersions(data.scorecardVersions)}
    })().catch(e=>{if(!cancelled)setError((e as Error).message)})
    return ()=>{cancelled=true}
  },[runId])
  if(!scorecard)return null
  const selected=versions.find(version=>version.run_id===runId)
  return <section aria-label="Scorecard versions" className="shrink-0 space-y-2">
    <div className="flex flex-wrap items-center gap-2 text-sm"><strong>{scorecard.name}</strong>
      <NativeSelect aria-label="Scorecard version" value={runId} onChange={event=>onSelect(event.target.value)} disabled={busy||saving}>
        {versions.map(version=><NativeSelectOption key={version.run_id} value={version.run_id}>Version {version.revision}{version.revision===scorecard.active_revision?' · active':''}</NativeSelectOption>)}
      </NativeSelect>
      {selected?.revision!==scorecard.active_revision?<Button size="sm" variant="outline" disabled={busy||saving} onClick={async()=>{
        setSaving(true);setError('')
        try{await graphql('mutation($id:ID!,$revision:Int!){activateScorecardVersion(scorecardId:$id,revision:$revision){id}}',{id:scorecard.id,revision:selected?.revision});setScorecard({...scorecard,active_revision:selected!.revision})}catch(e){setError((e as Error).message)}finally{setSaving(false)}
      }}>Use this version</Button>:null}
    </div>
    <details className="text-xs" onToggle={event=>{if(event.currentTarget.open)void graphql<{scorecardVersions:Version[]}>('query($id:ID!){scorecardVersions(scorecardId:$id)}',{id:scorecard.id}).then(result=>setVersions(result.scorecardVersions)).catch(e=>setError((e as Error).message))}}><summary className="cursor-pointer text-muted-foreground">Compare version metrics</summary>
      <div className="max-h-60 overflow-auto py-2"><table className="w-full text-left"><thead><tr>{['Version','Classifier','Recall','Precision','Accuracy','Labels'].map(name=><th key={name} className="p-2">{name}</th>)}</tr></thead><tbody>{versions.flatMap(version=>version.classifiers.map(classifier=>{
        const metrics=version.metrics[classifier.id],positive=classifier.config.classes.find(row=>row.role==='positive')?.label
        const mean=(key:'recall'|'precision')=>{const values=Object.values(metrics?.per_class??{}).map(row=>row[key]);return values.length&&values.every(value=>value!=null)?values.reduce<number>((sum,value)=>sum+(value??0),0)/values.length:null}
        return <tr key={`${version.revision}:${classifier.id}`} className="border-t border-border"><td className="p-2">{version.revision}</td><td className="p-2">{classifier.name}</td><td className="p-2">{percent(positive?metrics?.per_class[positive]?.recall:mean('recall'))}</td><td className="p-2">{percent(positive?metrics?.per_class[positive]?.precision:mean('precision'))}</td><td className="p-2">{percent(metrics?.accuracy)}</td><td className="p-2">{metrics?.count??0}</td></tr>
      }))}</tbody></table><p className="text-muted-foreground">Latest 200 reviewed items per classifier. Versions may cover different items; this is not a matched held-out comparison.</p></div>
    </details>{error?<p role="alert" className="text-destructive">{error}</p>:null}
  </section>
}
