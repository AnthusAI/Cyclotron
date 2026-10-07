import {Tabs,TabsContent,TabsList,TabsTrigger} from '@/components/ui/tabs'
import {useState} from 'react'

export function ExchangePanel({kind,title}:{kind:'decision'|'optimizer';title:string}) {
  const [view,setView]=useState('request')
  return <section id={`${kind}-exchange`} className="space-y-3" hidden aria-label={`${title} exchange`}>
    <h3 className="text-sm font-semibold">{title} · recorded exchange</h3>
    <p id={`${kind}-exchange-summary`} className="text-xs text-muted-foreground" />
    <Tabs value={view} onValueChange={setView}>
      <TabsList aria-label={`${title} request and response`} className="w-full">
        <TabsTrigger id={`${kind}-request-tab`} value="request">Request</TabsTrigger>
        <TabsTrigger id={`${kind}-response-tab`} value="response">Response{kind==='optimizer'?' / tool calls':''}</TabsTrigger>
      </TabsList>
      <TabsContent id={`${kind}-request-box`} value="request" forceMount hidden={view!=='request'}>
        <p className="mb-2 text-xs text-muted-foreground">{kind==='optimizer'?'Exact messages and full optimizer context':'Exact state, target, examples, rubric and questions'}</p>
        <pre id={`${kind}-request-content`} />
      </TabsContent>
      <TabsContent id={`${kind}-response-box`} value="response" forceMount hidden={view!=='response'}>
        <p className="mb-2 text-xs text-muted-foreground">{kind==='optimizer'?'Recorded output, tool calls and usage':'Recorded answers, probabilities and usage'}</p>
        <pre id={`${kind}-response-content`} />
      </TabsContent>
    </Tabs>
  </section>
}
