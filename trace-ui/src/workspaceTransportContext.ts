import {createContext,useContext} from 'react'
import {graphql,subscribeEvents,type TraceEvent} from './graphql'

export type WorkspaceTransport={
  graphql:<T>(query:string,variables?:Record<string,unknown>)=>Promise<T>
  subscribeEvents:(runId:string,after:number,onEvent:(event:TraceEvent)=>void,onStatus:(status:string)=>void)=>()=>void
  url:(path:string)=>string
  /** The hosted account every request is bound to; absent for a local, self-served workspace. */
  accountId?:string
}
const local:WorkspaceTransport={
  graphql:(query,variables)=>graphql(query,variables),
  subscribeEvents:(...args)=>subscribeEvents(...args),
  url:path=>path,
}
export const WorkspaceContext=createContext(local)
export function useWorkspaceTransport(){return useContext(WorkspaceContext)}
