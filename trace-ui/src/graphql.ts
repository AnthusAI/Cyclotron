export type TraceEvent={sequence:number;sourceId:string;payload:Record<string,unknown>}
export class WorkspaceAuthenticationRequired extends Error{}

export function workspaceUrl(path:string,accountId?:string){
  if(!accountId)return path
  const url=new URL(path,window.location.origin)
  url.searchParams.set('account',accountId)
  return url.pathname+url.search+url.hash
}

export async function graphql<T>(query:string,variables:Record<string,unknown>={},accountId?:string):Promise<T>{
  const started=performance.now()
  let status:number|'unavailable'='unavailable',successful=false
  try{
  const response=await fetch(workspaceUrl('/graphql',accountId),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({query,variables})})
  status=response.status
  if(response.status===401)throw new WorkspaceAuthenticationRequired('Authentication required')
  const body=await response.json()
  if(!response.ok||body.errors)throw new Error(body.errors?.[0]?.message||'The API request failed')
  successful=true
  return body.data as T
  }finally{
    // Fixed endpoint and timing only; never emit query, variables, account IDs,
    // response content or private error messages to browser diagnostics.
    console.debug('cyclotron-workspace-request '+JSON.stringify({path:'/graphql',status,successful,durationMs:Math.round(performance.now()-started)}))
  }
}

export function subscribeEvents(runId:string,after:number,onEvent:(event:TraceEvent)=>void,onStatus:(status:string)=>void,accountId?:string){
  let closed=false,socket:WebSocket|undefined,timer:ReturnType<typeof setTimeout>|undefined,handshakeTimer:ReturnType<typeof setTimeout>|undefined,cursor=after
  const reconnect=()=>{
    if(closed||timer)return
    onStatus('Reconnecting')
    timer=setTimeout(()=>{timer=undefined;connect()},1000)
  }
  const connect=()=>{
    if(closed)return
    let connection:WebSocket
    try{connection=new WebSocket(`${location.protocol==='https:'?'wss':'ws'}://${location.host}${workspaceUrl('/graphql',accountId)}`,'graphql-transport-ws')}
    catch{reconnect();return}
    socket=connection
    let acknowledged=false
    const active=()=>!closed&&socket===connection
    handshakeTimer=setTimeout(()=>{if(active()&&!acknowledged)connection.close()},10000)
    connection.onopen=()=>{if(active())connection.send(JSON.stringify({type:'connection_init'}))}
    connection.onmessage=message=>{
      if(!active())return
      let data:Record<string,unknown>
      try{data=JSON.parse(message.data)}catch{connection.close();return}
      if(data===null||typeof data!=='object')return
      if(data.type==='connection_ack'&&!acknowledged){
        acknowledged=true
        if(handshakeTimer)clearTimeout(handshakeTimer)
        handshakeTimer=undefined
        onStatus('Connected')
        connection.send(JSON.stringify({id:'events',type:'subscribe',payload:{query:'subscription($id:ID!,$after:Int!){runEvents(runId:$id,after:$after){sequence sourceId payload}}',variables:{id:runId,after:cursor}}}))
      }else if(data.type==='next'&&data.id==='events'&&acknowledged){
        const payload=data.payload as {data?:{runEvents?:TraceEvent};errors?:unknown[]}|undefined
        if(payload?.errors?.length){connection.close();return}
        const event=payload?.data?.runEvents
        if(event&&Number.isSafeInteger(event.sequence)&&event.sequence>cursor&&typeof event.sourceId==='string'&&
          event.payload!==null&&typeof event.payload==='object'&&!Array.isArray(event.payload)){
          cursor=event.sequence;onEvent(event)
        }
      }else if(data.type==='ping')connection.send(JSON.stringify({type:'pong'}))
      else if((data.type==='error'||data.type==='complete')&&data.id==='events')connection.close()
    }
    connection.onclose=event=>{
      if(!active())return
      if(handshakeTimer)clearTimeout(handshakeTimer)
      handshakeTimer=undefined
      socket=undefined
      if(event.code===4401||event.code===4403){closed=true;onStatus(event.code===4401?'Authentication required':'Event stream access denied');return}
      reconnect()
    }
    connection.onerror=()=>{if(active())connection.close()}
  }
  connect()
  return ()=>{
    closed=true
    if(timer)clearTimeout(timer)
    if(handshakeTimer)clearTimeout(handshakeTimer)
    if(socket?.readyState===WebSocket.OPEN)socket.send(JSON.stringify({id:'events',type:'complete'}))
    socket?.close()
    socket=undefined
  }
}
