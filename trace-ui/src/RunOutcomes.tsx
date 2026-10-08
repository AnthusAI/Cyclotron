import {ModelComparison} from './ModelComparison'
import {metricRates} from './metricRates'
import type {OutcomePoint,OutcomeWindow} from './recordedOutcomes'

const rows=[['Recall','recall',false],['Precision','precision',false],['Accuracy','accuracy',false],['Calibration error (ECE)','ece',true],['Brier score','brier',true]] as const
const format=(value:number|null,brier=false)=>value==null?'Unavailable':brier?value.toFixed(3):`${(value*100).toFixed(1)}%`
type Measures=Pick<OutcomePoint,'recall'|'precision'|'accuracy'|'ece'|'brier'>

function PairedTable({before,after}:{before:Measures;after:Measures}){
  return <table aria-label="Same-item raw and final performance" className="w-full text-left text-xs tabular-nums">
    <thead><tr>{['Metric','Raw decision model','Final classifier','Change'].map(label=><th scope="col" key={label}>{label}</th>)}</tr></thead>
    <tbody>{rows.map(([label,key,lower])=>{
      const prior=before[key],current=after[key],change=prior==null||current==null?null:current-prior
      return <tr key={key} className="border-t border-border"><th scope="row" className="py-1 font-medium">{label}{lower?<span className="ml-1 font-normal text-muted-foreground">↓ better</span>:null}</th><td>{format(prior,key==='brier')}</td><td>{format(current,key==='brier')}</td><td className={change==null||change===0?'text-muted-foreground':(lower?change<0:change>0)?'text-green-700 dark:text-green-400':'text-red-700 dark:text-red-400'}>{change==null?'Unavailable':`${change>0?'+':change<0?'−':''}${(Math.abs(change)*(key==='brier'?1:100)).toFixed(key==='brier'?3:1)}${key==='brier'?'':' pp'}`}</td></tr>
    })}</tbody>
  </table>
}

export function RunOutcomes({windows}:{windows:OutcomeWindow[]}){
  return <div className="space-y-3 pt-2">
    {windows.map(window=>{
      const distinct=window.first!==window.latest
      const paired=window.latest.rawFinal
      const measures=(output:NonNullable<typeof paired>['raw']):Measures=>({...metricRates(output,window.classes.length?window.classes:undefined),accuracy:output.accuracy,ece:output.calibration.ece,brier:output.calibration.brier??null})
      const describe=(point:OutcomePoint)=>`Cycle ${point.cycle??'unrecorded'} · ${point.count} label${point.count===1?'':'s'}`
      return <section key={window.id} className="space-y-1">
        {windows.length>1?<h3 className="text-xs font-semibold">{window.id}</h3>:null}
        {paired?<>
          <p className="text-xs text-muted-foreground">Latest raw model versus final classifier · same {paired.count} reviewed items · pre-vote predictions. This measures the outer classifier’s difference, not evidence of improvement caused by optimization over time.</p>
          <PairedTable before={measures(paired.raw)} after={measures(paired.final)}/>
          <p className="text-xs text-muted-foreground">Calibration uses {paired.raw.calibration.count} paired probability vectors. Small samples are noisy; these reviewed items are not a held-out test.</p>
          <details className="disclosure"><summary>Calibration curves & comparison details</summary><div className="p-2"><ModelComparison comparison={paired} classes={window.classes}/></div></details>
        </>:null}
        <details className="disclosure" open={paired?undefined:true}><summary>Recorded review-window trend</summary>
        <div className="space-y-1 p-2">
        <p className="text-xs text-muted-foreground">Recorded review-window trend, not a matched test of optimization. Different items and historical model versions may contribute; these changes do not prove an optimization gain. Small samples are noisy.</p>
        <table aria-label="Recorded performance change" className="w-full text-left text-xs tabular-nums">
          <thead><tr><th scope="col">Metric</th>{distinct?<th scope="col">First recorded<span className="block font-normal text-muted-foreground">{describe(window.first)}</span></th>:null}<th scope="col">Latest recorded<span className="block font-normal text-muted-foreground">{describe(window.latest)}</span></th>{distinct?<th scope="col">Change</th>:null}</tr></thead>
          <tbody>{rows.map(([label,key,lower])=>{
            const before=window.first[key],after=window.latest[key],change=before==null||after==null?null:after-before
            return <tr key={key} className="border-t border-border"><th scope="row" className="py-1 font-medium">{label}{lower?<span className="ml-1 font-normal text-muted-foreground">↓ better</span>:null}</th>{distinct?<td>{format(before,key==='brier')}</td>:null}<td>{format(after,key==='brier')}</td>{distinct?<td className={change==null||change===0?'text-muted-foreground':(lower?change<0:change>0)?'text-green-700 dark:text-green-400':'text-red-700 dark:text-red-400'}>{change==null?'Unavailable':`${change>0?'+':change<0?'−':''}${(Math.abs(change)*(key==='brier'?1:100)).toFixed(key==='brier'?3:1)}${key==='brier'?'':' pp'}`}</td>:null}</tr>
          })}</tbody>
        </table>
        <p className="text-xs text-muted-foreground">Calibration: {distinct?`${window.first.probabilityCount} → `:''}{window.latest.probabilityCount} probability vectors. Metrics use up to 200 most-recent reviewed labels.</p>
        </div></details>
      </section>
    })}
  </div>
}
