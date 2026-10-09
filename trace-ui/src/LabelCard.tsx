import {useState} from 'react'
import {ThumbsUp, ThumbsDown, SkipForward} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Badge} from '@/components/ui/badge'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {Label} from '@/components/ui/label'

export type CurrentItem={item:{id:string;title:string;abstract:string;submitted_at:string;categories:string[];authors:string;journal_ref?:string|null};prediction:{label:string;confidence:number;presentation_id:string}}

export function LabelCard({current,busy,onSubmit}:{current:CurrentItem;busy:boolean;onSubmit:(kind:string,payload:Record<string,unknown>)=>void}){
  const [comment,setComment]=useState('')
  const {item,prediction}=current
  const vote=(label:string)=>onSubmit('label',{item_id:item.id,label,comment:comment.trim()||null,presentation_id:prediction.presentation_id})
  return <Card className="gap-3">
    <CardHeader><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{item.submitted_at}</Badge>{item.categories.map(category=><Badge key={category} variant="secondary">{category}</Badge>)}</div><CardTitle className="mt-3 text-xl leading-snug">{item.title}</CardTitle><p className="text-sm text-muted-foreground">{item.authors}</p>{item.journal_ref?<p className="text-xs text-muted-foreground">{item.journal_ref}</p>:null}</CardHeader>
    <CardContent className="space-y-5"><p className="whitespace-pre-wrap text-sm leading-relaxed">{item.abstract}</p><div className="rounded-lg border border-border bg-muted/40 p-3 text-sm"><span className="text-muted-foreground">Flywheel predicts </span><strong>{prediction.label==='include'?'Include':'Exclude'} · {(prediction.confidence*100).toFixed(0)}%</strong></div><div className="space-y-2"><Label htmlFor="label-comment">Explanation (optional)</Label><textarea id="label-comment" value={comment} onChange={event=>setComment(event.target.value)} rows={3} placeholder="What made this relevant or irrelevant to you?" className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none focus:ring-2 focus:ring-ring" /></div><div className="flex flex-wrap gap-2"><Button disabled={busy} onClick={()=>vote('include')}><ThumbsUp />Include</Button><Button variant="outline" disabled={busy} onClick={()=>vote('exclude')}><ThumbsDown />Exclude</Button><Button variant="ghost" disabled={busy} onClick={()=>onSubmit('skip',{item_id:item.id})}><SkipForward />Skip</Button></div><p className="text-xs text-muted-foreground">Your label refers to the prediction shown above. Explanations guide the optimizer when this item belongs to training.</p></CardContent>
  </Card>
}
