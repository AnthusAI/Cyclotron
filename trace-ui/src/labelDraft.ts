import {useEffect,useState,type SetStateAction} from 'react'

type Draft={labels:Record<string,string>;comments:Record<string,string>}
const empty=():Draft=>({labels:{},comments:{}})
const strings=(value:unknown):value is Record<string,string>=>Boolean(value&&typeof value==='object'&&!Array.isArray(value)&&Object.values(value).every(row=>typeof row==='string'))
const read=(key:string):Draft=>{
  try{
    const value=JSON.parse(sessionStorage.getItem(key)??'null')
    return strings(value?.labels)&&strings(value?.comments)?value:empty()
  }catch{return empty()}
}

/** Key by immutable prediction presentation; drafts never count as recorded feedback. */
export function useLabelDraft(presentation:string):[Draft,(value:SetStateAction<Draft>)=>void]{
  const key=`cyclotron.label-draft.v1:${presentation}`
  const [draft,setDraft]=useState(()=>read(key))
  useEffect(()=>{
    try{sessionStorage.setItem(key,JSON.stringify(draft))}catch{/* A private browser must still allow labeling. */}
  },[key,draft])
  return [draft,setDraft]
}
