import {useEffect,useMemo,useRef,type ReactNode} from 'react'
import {graphql,subscribeEvents,workspaceUrl,WorkspaceAuthenticationRequired} from './graphql'
import {WorkspaceContext,type WorkspaceTransport} from './workspaceTransportContext'

/** Bind requests to this workspace, including callbacks retained by an old tab. */
export function WorkspaceTransportProvider({accountId,children,onAuthenticationRequired}:{accountId:string;children:ReactNode;onAuthenticationRequired?:()=>void}){
  const active=useRef(true)
  useEffect(()=>{active.current=true;return()=>{active.current=false}},[])
  const client=useMemo<WorkspaceTransport>(()=>({
    graphql:async(query,variables)=>{
      try{return await graphql(query,variables,accountId)}
      catch(error){
        if(active.current&&error instanceof WorkspaceAuthenticationRequired)onAuthenticationRequired?.()
        throw error
      }
    },
    subscribeEvents:(run,after,event,status)=>subscribeEvents(run,after,event,status,accountId),
    url:path=>workspaceUrl(path,accountId),
  }),[accountId,onAuthenticationRequired])
  return <WorkspaceContext.Provider value={client}>{children}</WorkspaceContext.Provider>
}
