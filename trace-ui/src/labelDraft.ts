import {useSessionDraft} from './sessionDraft'

type Draft={labels:Record<string,string>;comments:Record<string,string>}
const empty=():Draft=>({labels:{},comments:{}})
const strings=(value:unknown):value is Record<string,string>=>Boolean(value&&typeof value==='object'&&!Array.isArray(value)&&Object.values(value).every(row=>typeof row==='string'))
const valid=(value:unknown):value is Draft=>!!value&&typeof value==='object'&&strings((value as Draft).labels)&&strings((value as Draft).comments)

/** Key by immutable prediction presentation; drafts never count as recorded feedback. */
export function useLabelDraft(presentation:string){
  return useSessionDraft(`cyclotron.label-draft.v1:${presentation}`,empty,valid)
}
