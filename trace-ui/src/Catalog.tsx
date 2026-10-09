import {useEffect,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {Card,CardHeader,CardTitle,CardContent,CardFooter} from '@/components/ui/card'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'
import {graphql} from './graphql'
import {requestId} from './requestId'
import {ClassifierHistory,type ClassifierDefinition} from './ClassifierHistory'
import {ClassifierImpact} from './ClassifierImpact'

type Classifier=ClassifierDefinition
type ItemList={id:string;name:string;count:number}
type ListItem={id:string;revision:number;occurred_at:string;values:Record<string,unknown>}
type Vote={classifier_id:string;classifier_revision:number;label:string;comment:string}
type ItemResult={run_id:string;run_name:string;payload:{classifiers:Record<string,{label:string;confidence:number;name?:string}>}}

function ClassifierLabel({classifier,item,listId,previous,onSaved}:{classifier:Classifier;item:ListItem;listId:string;previous?:Vote;onSaved:()=>void}){
  const [label,setLabel]=useState(previous?.label??''),[comment,setComment]=useState(previous?.comment??'')
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[saved,setSaved]=useState(false)
  const save=async()=>{
    setBusy(true);setError('');setSaved(false)
    try{
      await graphql('mutation($classifier:ID!,$revision:Int!,$list:ID!,$item:String!,$itemRevision:Int!,$label:String!,$comment:String!,$request:String!){labelItem(classifierId:$classifier,classifierRevision:$revision,listId:$list,itemId:$item,itemRevision:$itemRevision,label:$label,comment:$comment,requestId:$request)}',
        {classifier:classifier.id,revision:classifier.revision,list:listId,item:item.id,itemRevision:item.revision,label,comment,request:requestId()})
      setSaved(true);onSaved()
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <Card className="gap-2"><CardHeader><CardTitle className="text-base">{classifier.name}</CardTitle><p className="text-sm text-muted-foreground">{classifier.config.question}</p></CardHeader><CardContent className="space-y-3">
    <Label htmlFor={`class-${classifier.id}`}>Classification</Label><NativeSelect id={`class-${classifier.id}`} value={label} onChange={e=>{setLabel(e.target.value);setSaved(false)}}><NativeSelectOption value="">Not labeled</NativeSelectOption>{classifier.config.classes.map(row=><NativeSelectOption key={row.label} value={row.label}>{row.label}</NativeSelectOption>)}</NativeSelect>
    <Label htmlFor={`comment-${classifier.id}`}>Explanation (optional)</Label><textarea id={`comment-${classifier.id}`} className="min-h-20 w-full rounded-md border border-input bg-background p-3 text-sm" value={comment} onChange={e=>{setComment(e.target.value);setSaved(false)}} />
    <div className="flex items-center gap-3"><Button disabled={busy||!label} onClick={save}>{previous?'Save correction':'Save label'}</Button>{saved?<span role="status" className="text-sm text-muted-foreground">Saved</span>:null}</div>{error?<p role="alert">{error}</p>:null}
  </CardContent></Card>
}

export function Catalog({section,cyclotronId,cyclotronRevision,readOnly=false,onChanged}:{section:'classifiers'|'items';cyclotronId?:string;cyclotronRevision?:number;readOnly?:boolean;onChanged?:()=>void}){
  const [classifiers,setClassifiers]=useState<Classifier[]>([]),[lists,setLists]=useState<ItemList[]>([])
  const [selectedList,setSelectedList]=useState(''),[items,setItems]=useState<ListItem[]>([]),[item,setItem]=useState<ListItem|null>(null),[votes,setVotes]=useState<Vote[]>([])
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[editing,setEditing]=useState<Classifier|null>(null),[creating,setCreating]=useState(false)
  const [name,setName]=useState(''),[question,setQuestion]=useState(''),[classes,setClasses]=useState(''),[positive,setPositive]=useState('')
  const [listName,setListName]=useState(''),[page,setPage]=useState(0)
  const [results,setResults]=useState<ItemResult[]>([])
  const [history,setHistory]=useState<Classifier|null>(null)
  const editClassifier=(classifier:Classifier)=>{
    setHistory(null);setEditing(classifier);setCreating(true);setName(classifier.name)
    setQuestion(classifier.config.question);setClasses(classifier.config.classes.map(row=>row.label).join('\n'))
    setPositive(classifier.config.classes.find(row=>row.role==='positive')?.label??'')
  }
  const refresh=async()=>{
    const result=await graphql<{classifiers:Classifier[];itemLists:ItemList[]}>('{classifiers itemLists}')
    const members=cyclotronId?await graphql<{cyclotronClassifiers:Classifier[]}>('query($id:ID!,$revision:Int){cyclotronClassifiers(cyclotronId:$id,revision:$revision)}',{id:cyclotronId,revision:cyclotronRevision??null}):null
    setClassifiers(members?.cyclotronClassifiers??result.classifiers);setLists(result.itemLists);onChanged?.()
  }
  useEffect(()=>{let cancelled=false;Promise.all([graphql<{classifiers:Classifier[];itemLists:ItemList[]}>('{classifiers itemLists}'),cyclotronId?graphql<{cyclotronClassifiers:Classifier[]}>('query($id:ID!,$revision:Int){cyclotronClassifiers(cyclotronId:$id,revision:$revision)}',{id:cyclotronId,revision:cyclotronRevision??null}):Promise.resolve(null)]).then(([result,members])=>{if(!cancelled){setClassifiers(members?.cyclotronClassifiers??result.classifiers);setLists(result.itemLists)}}).catch(e=>{if(!cancelled)setError(e.message)});return()=>{cancelled=true}},[cyclotronId,cyclotronRevision])
  useEffect(()=>{
    if(!selectedList)return
    let cancelled=false;setItems([]);setItem(null)
    graphql<{listItems:ListItem[]}>('query($list:ID!,$after:Int!){listItems(listId:$list,after:$after)}',{list:selectedList,after:page*200}).then(result=>{if(!cancelled)setItems(result.listItems)}).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[selectedList,page])
  const loadVotes=async()=>{
    if(!item)return
    const result=await graphql<{itemLabels:Vote[]}>('query($list:ID!,$item:String!,$revision:Int!){itemLabels(listId:$list,itemId:$item,itemRevision:$revision)}',{list:selectedList,item:item.id,revision:item.revision})
    setVotes(result.itemLabels)
  }
  useEffect(()=>{
    setVotes([]);setResults([]);if(!item)return
    let cancelled=false
    graphql<{itemLabels:Vote[]}>('query($list:ID!,$item:String!,$revision:Int!){itemLabels(listId:$list,itemId:$item,itemRevision:$revision)}',{list:selectedList,item:item.id,revision:item.revision}).then(result=>{if(!cancelled)setVotes(result.itemLabels)}).catch(e=>{if(!cancelled)setError(e.message)})
    graphql<{itemResults:ItemResult[]}>('query($list:ID!,$item:String!,$revision:Int!){itemResults(listId:$list,itemId:$item,itemRevision:$revision)}',{list:selectedList,item:item.id,revision:item.revision}).then(result=>{if(!cancelled)setResults(result.itemResults??[])}).catch(e=>{if(!cancelled)setError(e.message)})
    return()=>{cancelled=true}
  },[item,selectedList])
  const saveClassifier=async()=>{
    setBusy(true);setError('')
    try{
      const labels=classes.split('\n').map(value=>value.trim()).filter(Boolean)
      await graphql('mutation($id:String!,$name:String!,$config:JSON!){saveClassifier(identifier:$id,name:$name,config:$config)}',
        {id:editing?.id??requestId(),name,config:{...editing?.config,question,classes:labels.map(label=>({label,...(positive?{role:label===positive?'positive':'negative'}:{})}))}})
      setCreating(false);setEditing(null);await refresh()
    }catch(e){setError((e as Error).message)}finally{setBusy(false)}
  }
  return <main className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto p-5 [&>*]:shrink-0">
    {history?<ClassifierHistory key={history.id} classifier={history} onClose={()=>setHistory(null)} onEdit={readOnly?undefined:editClassifier}/>:null}
<div className="flex items-center justify-between"><h1 className="text-xl font-semibold">{section==='classifiers'?'Classifiers':'Item lists'}</h1>{section==='classifiers'&&!cyclotronId?<Button onClick={()=>{setCreating(true);setEditing(null);setName('');setQuestion('');setClasses('');setPositive('')}}>New classifier</Button>:null}</div>
    {error?<p role="alert" className="text-sm text-destructive">{error}</p>:null}
    {section==='items'&&item&&results.length?<Card><CardHeader><CardTitle className="text-base">Classifier results · {String(item.values.title??item.id)}</CardTitle></CardHeader><CardContent className="space-y-3">{results.map(result=><section key={result.run_id}><p className="text-xs text-muted-foreground">{result.run_name}</p><div className="flex flex-wrap gap-3">{Object.entries(result.payload.classifiers).map(([id,prediction])=><p key={id} className="text-sm">{prediction.name??id}: <strong>{prediction.label}</strong> · {(prediction.confidence*100).toFixed(1)}%</p>)}</div></section>)}</CardContent></Card>:null}
    {section==='classifiers'?<>
      {creating&&editing?<ClassifierImpact key={editing.id} classifierId={editing.id}/>:null}
      {creating?<Card className="max-h-[calc(100dvh-10rem)] shrink-0"><CardHeader className="shrink-0"><div className="flex flex-wrap items-center justify-between gap-2"><CardTitle>{editing?'New configuration revision':'Set up a classifier'}</CardTitle><Button disabled={busy||!name.trim()||!question.trim()} onClick={saveClassifier}>Save changes</Button></div></CardHeader><CardContent className="grid min-h-0 max-w-2xl gap-3 overflow-y-auto"><Label htmlFor="classifier-name">Name</Label><Input id="classifier-name" value={name} onChange={e=>setName(e.target.value)} /><Label htmlFor="classifier-question">Decision question</Label><Input id="classifier-question" value={question} onChange={e=>setQuestion(e.target.value)} /><Label htmlFor="classifier-classes">Ordered classes (one per line)</Label><textarea id="classifier-classes" className="min-h-28 rounded-md border border-input bg-background p-3 text-sm" value={classes} onChange={e=>setClasses(e.target.value)} /><Label htmlFor="positive-class">Positive class (optional, for precision / recall)</Label><NativeSelect id="positive-class" value={positive} onChange={e=>setPositive(e.target.value)}><NativeSelectOption value="">No binary roles</NativeSelectOption>{classes.split('\n').map(s=>s.trim()).filter(Boolean).map(label=><NativeSelectOption key={label} value={label}>{label}</NativeSelectOption>)}</NativeSelect></CardContent><CardFooter className="shrink-0 gap-2"><Button disabled={busy||!name.trim()||!question.trim()} onClick={saveClassifier}>Save classifier</Button><Button variant="ghost" onClick={()=>setCreating(false)}>Cancel</Button></CardFooter></Card>:null}
      <div className="grid gap-4 lg:grid-cols-2">{classifiers.map(classifier=><Card key={classifier.id}><CardHeader><CardTitle>{classifier.name}</CardTitle><p className="text-xs text-muted-foreground">Revision {classifier.revision}</p></CardHeader><CardContent className="space-y-3"><p className="text-sm">{classifier.config.question}</p><div className="flex flex-wrap gap-2">{classifier.config.classes.map(row=><span key={row.label} className="rounded-md bg-muted px-2 py-1 text-xs">{row.label}{row.role?` · ${row.role}`:''}</span>)}</div><div className="flex flex-wrap gap-2">{!readOnly?<Button variant="outline" onClick={()=>editClassifier(classifier)}>Edit configuration</Button>:null}<Button variant="ghost" onClick={()=>setHistory(classifier)}>View history</Button></div></CardContent></Card>)}</div>
      {!classifiers.length?<p className="text-sm text-muted-foreground">Create a classifier with its question and possible classes before review.</p>:null}
    </>:<>
      <form className="flex max-w-xl gap-2" onSubmit={async e=>{e.preventDefault();setBusy(true);try{await graphql('mutation($id:String!,$name:String!){saveItemList(identifier:$id,name:$name)}',{id:requestId(),name:listName});setListName('');await refresh()}catch(e){setError((e as Error).message)}finally{setBusy(false)}}}><Input aria-label="New item list name" placeholder="Name a new item list" value={listName} onChange={e=>setListName(e.target.value)} /><Button disabled={busy||!listName.trim()}>Create list</Button></form>
      <div className="flex flex-wrap gap-2">{lists.map(list=><Button key={list.id} variant={selectedList===list.id?'secondary':'outline'} onClick={()=>{setSelectedList(list.id);setPage(0)}}>{list.name} · {list.count} items</Button>)}</div>
      {selectedList?<div className="grid min-h-0 gap-5 lg:grid-cols-[280px_minmax(0,1fr)]"><aside className="space-y-2"><p className="text-xs text-muted-foreground">Oldest first · page {page+1}</p>{items.map(row=><button key={row.id} className={`w-full rounded-lg border p-3 text-left text-sm ${item?.id===row.id?'border-primary bg-accent':'border-border hover:bg-accent'}`} onClick={()=>setItem(row)}>{String(row.values.title??row.id)}</button>)}<div className="flex gap-2"><Button variant="outline" disabled={!page} onClick={()=>setPage(p=>p-1)}>Previous</Button><Button variant="outline" disabled={items.length<200} onClick={()=>setPage(p=>p+1)}>Next</Button></div></aside><section className="space-y-4">{item?<><Card><CardHeader><CardTitle>{String(item.values.title??item.id)}</CardTitle><p className="text-xs text-muted-foreground">{item.occurred_at} · {item.id} · revision {item.revision}</p></CardHeader><CardContent><dl className="space-y-3">{Object.entries(item.values).filter(([key])=>key!=='title'&&key!=='text').map(([key,value])=><div key={key}><dt className="text-xs capitalize text-muted-foreground">{key.replaceAll('_',' ')}</dt><dd className="whitespace-pre-wrap text-sm">{typeof value==='string'?value:JSON.stringify(value)}</dd></div>)}{Object.keys(item.values).length===1?<p className="whitespace-pre-wrap text-sm">{String(item.values.text??'')}</p>:null}</dl></CardContent></Card><p className="text-sm text-muted-foreground">Label this item independently for each classifier. Saving a correction retains the earlier label in history.</p>{classifiers.map(classifier=><ClassifierLabel key={`${item.id}:${item.revision}:${classifier.id}:${classifier.revision}:${votes.find(v=>v.classifier_id===classifier.id&&v.classifier_revision===classifier.revision)?.label??''}`} classifier={classifier} item={item} listId={selectedList} previous={votes.find(v=>v.classifier_id===classifier.id&&v.classifier_revision===classifier.revision)} onSaved={()=>loadVotes().catch(e=>setError(e.message))} />)}</>:<p className="text-sm text-muted-foreground">Select an item to label it for your classifiers.</p>}</section></div>:null}
    </>}
  </main>
}
