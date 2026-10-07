import {Check,LoaderCircle,TriangleAlert} from 'lucide-react'

export type LabelSubmission={runId:string;count:number;jobId?:string;unconfirmed?:boolean}
export type SubmissionJob={id:string;kind:string;status:string;result?:Record<string,unknown>|null}

export function SubmissionFeedback({submission,jobs,compact=false}:{submission:LabelSubmission;jobs:SubmissionJob[];compact?:boolean}){
  const job=jobs.find(job=>job.id===submission.jobId)
  const failed=submission.unconfirmed||['failed','interrupted'].includes(job?.status??'')
  const saved=job?.status==='completed'
  const preparing=saved&&jobs.some(job=>job.kind==='prepare'&&['pending','running'].includes(job.status))
  const waiting=!failed&&(!saved||preparing)
  const warnings=saved&&Array.isArray(job?.result?.optimization_warnings)?job.result.optimization_warnings as {reason:string}[]:[]
  const count=`${submission.count} ${submission.count===1?'label':'labels'}`
  const title=failed?'Label submission needs attention':saved?`${count} recorded${preparing?' · preparing next item…':''}`:
    !submission.jobId?`Sending ${count}…`:job?.status==='running'?'Recording labels and updating the flywheel…':`${count} received · waiting to record`
  const explanation=failed?'Your submission could not be confirmed as complete. Check the error below before submitting again.':saved?
    'Your feedback is saved.':
    'Please wait. You do not need to submit again; your selections and explanations stay here while we process them.'
  return <div role={failed?'alert':'status'} aria-live="polite" aria-atomic="true" className={`flex shrink-0 items-start gap-3 ${compact?'py-1':'rounded-xl border p-4'} ${compact?'':failed?'border-destructive/40 bg-destructive/5':saved?'border-emerald-500/30 bg-emerald-500/5':'border-primary/25 bg-accent/50'}`}>
    {waiting?<span className="motion-safe:animate-spin"><LoaderCircle aria-hidden="true" className="size-5" /></span>:failed?<TriangleAlert aria-hidden="true" className="size-5 text-destructive" />:<Check aria-hidden="true" className="size-5 text-emerald-600 dark:text-emerald-400" />}
    <div><p className="text-sm font-medium">{title}</p>{!compact||failed?<p className="mt-1 text-xs text-muted-foreground">{explanation}</p>:null}{warnings.length?<p className="mt-2 text-xs text-amber-700 dark:text-amber-400">Optimization paused: {[...new Set(warnings.map(warning=>warning.reason))].join('; ')}. Your labels are saved.</p>:null}</div>
  </div>
}
