import {requestId} from './requestId'

const storageKey='cyclotron.commands.v1'
type CommandStorage=Pick<Storage,'getItem'|'setItem'>
function canonical(value:unknown):unknown{
  if(Array.isArray(value))return value.map(canonical)
  if(value&&typeof value==='object')return Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical((value as Record<string,unknown>)[key])]))
  return value
}

/** Keep only commands whose acknowledgement is uncertain, never retry them automatically. */
export class PendingCommandIds{
  private entries=new Map<string,string>()
  private storage:CommandStorage|null
  constructor(storage?:CommandStorage|null){
    this.storage=storage??null
    try{
      if(storage===undefined)this.storage=window.sessionStorage
      const saved=JSON.parse(this.storage?.getItem(storageKey)??'[]') as unknown
      if(Array.isArray(saved)&&saved.every(row=>Array.isArray(row)&&row.length===2&&row.every(value=>typeof value==='string'&&value.length>0)))
        this.entries=new Map(saved as [string,string][])
    }catch{/* Session storage is optional; in-memory idempotency still works. */}
  }
  private persist(){
    try{this.storage?.setItem(storageKey,JSON.stringify([...this.entries]))}catch{/* Retain uncertain identities in memory. */}
  }
  identity(runId:string,kind:string,payload:Record<string,unknown>):string{
    const normalized=JSON.parse(JSON.stringify(payload)) as unknown
    const key=JSON.stringify([runId,kind,canonical(normalized)])
    let id=this.entries.get(key)
    if(!id){id=requestId();this.entries.set(key,id);this.persist()}
    return id
  }
  acknowledge(id:string){
    for(const [key,value] of this.entries)if(value===id)this.entries.delete(key)
    this.persist()
  }
}
