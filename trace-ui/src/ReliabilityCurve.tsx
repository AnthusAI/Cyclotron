import {useEffect,useState} from 'react'

export type CalibrationCurve={count:number;missing_probability_count?:number;ece:number|null;bins:{count:number;mean_confidence:number|null;accuracy:number|null}[];brier?:number|null;log_loss?:number|null;source_counts?:Record<string,number>;version_counts?:Record<string,number>;samples?:unknown[];matched_head_comparison?:{raw:CalibrationCurve;calibrated:CalibrationCurve}}
export function ReliabilityCurve({curve}:{curve?:CalibrationCurve}){
  if(!curve)return <p className="text-xs text-muted-foreground">No calibration snapshot recorded at this point.</p>
  const points=curve.bins.filter(bin=>bin.count>0&&bin.mean_confidence!=null&&bin.accuracy!=null)
  return <div className="space-y-1">
    <svg viewBox="0 0 240 155" role="img" aria-label="Confidence calibration curve" className="w-full max-w-xs">
      <path d="M30 10V125H230 M30 125L230 10" fill="none" stroke="currentColor" opacity=".25" />
      <polyline points={points.map(bin=>`${30+200*bin.mean_confidence!},${125-115*bin.accuracy!}`).join(' ')} fill="none" stroke="#22c55e" strokeWidth="2" />
      {points.map((bin,i)=><circle key={i} cx={30+200*bin.mean_confidence!} cy={125-115*bin.accuracy!} r="3" fill="#22c55e"><title>{bin.count} predictions · confidence {Math.round(bin.mean_confidence!*100)}% · correct {Math.round(bin.accuracy!*100)}%</title></circle>)}
      <text x="100" y="149" fontSize="10" fill="currentColor">Confidence →</text>
      <text x="3" y="12" fontSize="9" fill="currentColor">100%</text><text x="10" y="125" fontSize="9" fill="currentColor">0%</text>
    </svg>
    <p className="text-xs text-muted-foreground">{curve.count} with probabilities · calibration error {curve.ece==null?'—':`${(curve.ece*100).toFixed(1)}%`} (lower is better). Diagonal: perfect reliability. Small samples are noisy.</p>
    {curve.missing_probability_count?<p className="text-xs text-muted-foreground">{curve.missing_probability_count} reviewed predictions have no probability vector. They count toward agreement, not calibration.</p>:null}
    {curve.source_counts?<p className="text-xs text-muted-foreground">{Object.entries(curve.source_counts).map(([source,count])=>`${source}: ${count}`).join(' · ')}. Recorded predictions, not a refit on this window.</p>:null}
    {curve.brier!=null?<p className="text-xs text-muted-foreground">Brier {curve.brier.toFixed(3)} · Log loss {curve.log_loss?.toFixed(3)??'—'}</p>:null}
    {curve.matched_head_comparison?<details><summary className="text-xs">Matched ML calibration comparison</summary><p className="text-xs">{curve.matched_head_comparison.raw.count} fitted-head samples · raw ECE {((curve.matched_head_comparison.raw.ece??0)*100).toFixed(1)}% → calibrated ECE {((curve.matched_head_comparison.calibrated.ece??0)*100).toFixed(1)}%</p></details>:null}
    {curve.samples?<details><summary className="text-xs">Snapshot provenance</summary><pre className="max-h-48 overflow-auto text-xs">{JSON.stringify({versions:curve.version_counts,samples:curve.samples},null,2)}</pre></details>:null}
  </div>
}

export function PlaybackCalibration(){
  const [snapshots,setSnapshots]=useState<Record<string,CalibrationCurve>>({})
  useEffect(()=>{
    const update=(event:Event)=>setSnapshots((event as CustomEvent<Record<string,CalibrationCurve>>).detail)
    window.addEventListener('flywheel-calibration-position',update)
    return ()=>window.removeEventListener('flywheel-calibration-position',update)
  },[])
  return <details className="disclosure"><summary>Confidence calibration at this cycle</summary>{Object.keys(snapshots).length?Object.entries(snapshots).map(([id,curve])=><section key={id}><h3 className="text-sm font-semibold">{id}</h3><ReliabilityCurve curve={curve}/></section>):<ReliabilityCurve/>}</details>
}
