import {useState} from 'react'
import {Button} from '@/components/ui/button'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {graphql} from './graphql'
import {requestId} from './requestId'

export function ComparisonResume({runId,ceiling,busy,enabled,status}:{runId:string;ceiling:number;busy:boolean;enabled:boolean;status:string}){
  const [limit,setLimit]=useState(ceiling),[retryFailed,setRetryFailed]=useState(false),[confirmed,setConfirmed]=useState(false)
  const [identity,setIdentity]=useState(requestId),[sending,setSending]=useState(false),[queued,setQueued]=useState(false),[error,setError]=useState('')
  if(!['failed','interrupted'].includes(status))return null
  const changed=()=>{setConfirmed(false);setIdentity(requestId());setError('')}
  const submit=async()=>{
    if(!confirmed||!Number.isInteger(limit)||limit<ceiling)return
    setSending(true);setError('')
    try{
      await graphql('mutation($run:ID!,$request:String!,$ceiling:Int!,$retryFailed:Boolean!,$confirmed:Boolean!){resumeMatchedComparison(runId:$run,requestId:$request,maxRequests:$ceiling,retryFailed:$retryFailed,confirmed:$confirmed){id status}}',
        {run:runId,request:identity,ceiling:limit,retryFailed,confirmed:true})
      setQueued(true)
    }catch(e){setError((e as Error).message)}finally{setSending(false)}
  }
  const locked=busy||sending||queued
  return <section aria-label="Resume comparison" className="shrink-0 space-y-3 rounded-lg border p-4">
    <p className="text-sm font-semibold">Comparison {status}</p>
    <p className="text-xs text-muted-foreground">Frozen inputs and complete responses are retained. This ceiling includes every previous attempt, including failed calls. Resume does not start a new learning run.</p>
    <Label htmlFor="resume-ceiling">Total request ceiling</Label><Input id="resume-ceiling" type="number" min={ceiling} value={limit} disabled={locked} onChange={event=>{setLimit(Number(event.target.value));changed()}} />
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={retryFailed} disabled={locked} onChange={event=>{setRetryFailed(event.target.checked);changed()}} />Allow retry of failed or interrupted model calls</label>
    <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={confirmed} disabled={locked} onChange={event=>setConfirmed(event.target.checked)} />Authorize paid resume requests</label>
    <Button disabled={locked||!enabled||!confirmed||!Number.isInteger(limit)||limit<ceiling} onClick={submit}>{sending?'Submitting…':'Resume comparison'}</Button>
    {queued?<p role="status" className="text-xs">Resume queued. Cached responses will be reused.</p>:null}
    {error?<p role="alert" className="text-sm text-destructive">{error} Submission could not be confirmed. Retrying unchanged controls uses the same identity.</p>:null}
  </section>
}
