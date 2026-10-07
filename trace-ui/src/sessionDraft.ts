import {useCallback,useEffect,useState,type SetStateAction} from 'react'

/** Browser-session drafts are local intent, never recorded feedback. */
export function useSessionDraft<T>(key:string,empty:()=>T,valid:(value:unknown)=>value is T):[T,(value:SetStateAction<T>)=>void]{
  const read=():T=>{
    try{const value:unknown=JSON.parse(sessionStorage.getItem(key)??'null');return valid(value)?value:empty()}
    catch{return empty()}
  }
  const [stored,setStored]=useState(()=>({key,draft:read()}))
  // Replace scoped state before children commit, not in a later reset effect.
  if(stored.key!==key)setStored({key,draft:read()})
  useEffect(()=>{
    try{sessionStorage.setItem(stored.key,JSON.stringify(stored.draft))}catch{/* Editing must work without browser storage. */}
  },[stored])
  const setDraft=useCallback((value:SetStateAction<T>)=>setStored(previous=>{
    if(previous.key!==key)return previous
    return {key,draft:typeof value==='function'?(value as (draft:T)=>T)(previous.draft):value}
  }),[key])
  return [stored.draft,setDraft]
}
