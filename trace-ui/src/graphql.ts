export type TraceEvent={sequence:number;sourceId:string;payload:Record<string,unknown>}

export async function graphql<T>(query:string,variables:Record<string,unknown>={}):Promise<T>{
  const response=await fetch('/graphql',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,variables})})
  if(response.status===401)throw new Error('Authentication required')
  const body=await response.json()
  if(!response.ok||body.errors)throw new Error(body.errors?.[0]?.message||'The API request failed')
  return body.data as T
}

export function subscribeEvents(runId:string,after:number,onEvent:(event:TraceEvent)=>void,onStatus:(status:string)=>void){
  let closed=false,socket:WebSocket|undefined,timer:ReturnType<typeof setTimeout>|undefined,cursor=after
  const connect=()=>{
    if(closed)return
    socket=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}/graphql`,'graphql-transport-ws')
    socket.onopen=()=>socket?.send(JSON.stringify({type:'connection_init'}))
    socket.onmessage=message=>{
      const data=JSON.parse(message.data)
      if(data.type==='connection_ack'){
        onStatus('Connected')
        socket?.send(JSON.stringify({id:'events',type:'subscribe',payload:{query:'subscription($id:ID!,$after:Int!){runEvents(runId:$id,after:$after){sequence sourceId payload}}',variables:{id:runId,after:cursor}}}))
      }else if(data.type==='next'){
        const event=data.payload.data?.runEvents as TraceEvent|undefined
        if(event&&event.sequence>cursor){cursor=event.sequence;onEvent(event)}
      }else if(data.type==='ping')socket?.send(JSON.stringify({type:'pong'}))
    }
    socket.onclose=()=>{if(!closed){onStatus('Reconnecting');timer=setTimeout(connect,1000)}}
    socket.onerror=()=>socket?.close()
  }
  connect()
  return ()=>{closed=true;if(timer)clearTimeout(timer);socket?.close()}
}
