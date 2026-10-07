import {useSessionDraft} from './sessionDraft'

export type CorrectionVote={id:string;item:string;classifier:string;label:string;comment:string;readonly:boolean}
export type CorrectionEdit={rows:CorrectionVote[];draft:Record<string,{label:string;comment:string}>}
export type CorrectionDraft={item:string;edits:Record<string,CorrectionEdit>}
const empty=():CorrectionDraft=>({item:'',edits:{}})
const record=(value:unknown):value is Record<string,unknown>=>!!value&&typeof value==='object'&&!Array.isArray(value)
const text=(value:unknown):value is string=>typeof value==='string'&&value.length>0
const valid=(value:unknown):value is CorrectionDraft=>{
  if(!record(value)||typeof value.item!=='string'||!record(value.edits))return false
  return Object.entries(value.edits).every(([item,edit])=>{
    if(!item||!record(edit)||!Array.isArray(edit.rows)||!record(edit.draft))return false
    const classifiers=new Set<string>()
    for(const row of edit.rows){
      if(!record(row)||!text(row.id)||row.item!==item||!text(row.classifier)||!text(row.label)||typeof row.comment!=='string'||typeof row.readonly!=='boolean'||classifiers.has(row.classifier))return false
      classifiers.add(row.classifier)
    }
    return Object.entries(edit.draft).every(([classifier,draft])=>classifiers.has(classifier)&&record(draft)&&text(draft.label)&&typeof draft.comment==='string')
  })
}

export function useCorrectionDraft(runId:string){
  return useSessionDraft(`cyclotron.correction-draft.v1:${runId}`,empty,valid)
}
