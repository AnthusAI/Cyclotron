import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import type {TraceEvent} from './graphql'
const readable=(value:unknown)=>{
  if(typeof value==='string'){try{return JSON.stringify(JSON.parse(value),null,2)}catch{return value}}
  return JSON.stringify(value,null,2)
}

export function TraceDetail({event}:{event:TraceEvent}){
  const value=event.payload
  const messages=Array.isArray(value.messages)?value.messages as {role:string;content:unknown}[]:null
  return <Card className="gap-2"><CardHeader><CardTitle className="text-sm capitalize">{String(value.kind).replaceAll('-',' ')}</CardTitle><p className="text-xs text-muted-foreground">Event {String(value.event_id??event.sequence)} · {String(value.created_at??'')}</p></CardHeader><CardContent className="space-y-3">
    {messages?messages.map((message,index)=><details key={index} className="disclosure" open={index===messages.length-1}><summary>{message.role} · full request context</summary><pre>{readable(message.content)}</pre></details>):null}
    {value.state?<details className="disclosure" open><summary>Decision state (actual examples and rubric)</summary><pre>{readable(value.state)}</pre></details>:null}
    {value.request?<details className="disclosure" open><summary>Exact cached decision request</summary><pre>{readable(value.request)}</pre></details>:null}
    {value.questions?<details className="disclosure"><summary>Classification questions</summary><pre>{readable(value.questions)}</pre></details>:null}
    {value.content?<details className="disclosure" open><summary>Model response</summary><pre>{readable(value.content)}</pre></details>:null}
    {value.answers?<details className="disclosure" open><summary>Decision answers</summary><pre>{readable(value.answers)}</pre></details>:null}
    {value.tool_calls?<details className="disclosure"><summary>Tool calls</summary><pre>{readable(value.tool_calls)}</pre></details>:null}
    <details className="disclosure"><summary>Exact stored event</summary><pre>{readable(value)}</pre></details>
  </CardContent></Card>
}
