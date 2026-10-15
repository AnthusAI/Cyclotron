import type {TraceEvent} from './graphql'

/** Explicit hosted HTTP event transport; each bounded page is freshly authorized. */
export function pollEvents(runId:string,after:number,onEvent:(event:TraceEvent)=>void,onStatus:(status:string)=>void,
  accountId?:string,onAuthorityLost?:()=>void){
  if(!Number.isSafeInteger(after)||after<0)throw new Error('Invalid event cursor')
  let closed=false,cursor=after,retry=2000,timer:ReturnType<typeof setTimeout>|undefined
  let active:AbortController|undefined,deadline:ReturnType<typeof setTimeout>|undefined
  const schedule=(delay:number)=>{if(!closed)timer=setTimeout(()=>{timer=undefined;void poll()},delay)}
  async function poll(){
    if(closed)return
    const controller=new AbortController();active=controller
    deadline=setTimeout(()=>controller.abort(),10000)
    try{
      const query=new URLSearchParams({after:String(cursor),limit:'100'})
      if(accountId)query.set('account',accountId)
      const response=await fetch(`/api/runs/${encodeURIComponent(runId)}/events?${query}`,{
        method:'GET',credentials:'same-origin',cache:'no-store',signal:controller.signal})
      if(closed)return
      if(response.status===401||response.status===403){
        closed=true;onStatus(response.status===401?'Authentication required':'Event stream access denied');onAuthorityLost?.();return
      }
      if(!response.ok)throw new Error('Event transport unavailable')
      const text=await response.text()
      if(closed)return
      if(text.length>1048576)throw new Error('Event page exceeds limit')
      const body:unknown=JSON.parse(text)
      if(!body||typeof body!=='object'||!('events' in body)||!Array.isArray(body.events)||body.events.length>100)throw new Error('Invalid event page')
      let previous=-1
      for(const event of body.events){
        if(!event||typeof event!=='object'||!Number.isSafeInteger(event.sequence)||event.sequence<1||event.sequence<=previous||
          typeof event.sourceId!=='string'||!event.sourceId||event.sourceId.length>1024||
          !event.payload||typeof event.payload!=='object'||Array.isArray(event.payload))throw new Error('Invalid event page')
        previous=event.sequence
      }
      // Validate the whole page before delivering anything or advancing its cursor.
      for(const event of body.events as TraceEvent[]){
        if(closed)return
        if(event.sequence>cursor){onEvent(event);cursor=event.sequence}
      }
      onStatus('Connected (polling)');retry=2000
      schedule(body.events.length===100?50:2000)
    }catch{
      if(!closed){onStatus('Reconnecting');schedule(retry);retry=Math.min(30000,retry*2)}
    }finally{
      if(deadline)clearTimeout(deadline)
      deadline=undefined;if(active===controller)active=undefined
    }
  }
  void poll()
  return ()=>{closed=true;if(timer)clearTimeout(timer);if(deadline)clearTimeout(deadline);active?.abort()}
}
