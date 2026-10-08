export type CalibrationBin={count:number;mean_confidence:number|null;accuracy:number|null}
type Output={name:string;bins:CalibrationBin[]}
const percent=(value:number)=>`${(value*100).toFixed(1)}%`

/** Touch and keyboard alternative to the graph's hover-only SVG titles. */
export function CalibrationBins({outputs}:{outputs:Output[]}){
  const rows=outputs.flatMap(output=>output.bins.flatMap((bin,index)=>
    bin.count>0&&bin.mean_confidence!=null&&bin.accuracy!=null
      ? [{key:`${output.name}-${index}`,name:output.name,confidence:bin.mean_confidence,
          correctness:bin.accuracy,count:bin.count}] : []))
  if(!rows.length)return null
  return <details className="disclosure">
    <summary className="flex min-h-11 cursor-pointer items-center text-xs">Calibration bins</summary>
    <div className="overflow-x-auto rounded-md border" role="region" aria-label="Calibration bins" tabIndex={0}>
      <table aria-label="Calibration bin details" className="w-full min-w-80 text-left text-xs tabular-nums">
        <thead><tr>{['Output','Mean confidence','Observed correctness','Samples'].map(label=>
          <th key={label} scope="col" className="p-2 font-medium">{label}</th>)}</tr></thead>
        <tbody>{rows.map(row=><tr key={row.key} className="border-t">
          <th scope="row" className="p-2 font-medium">{row.name}</th>
          <td className="p-2">{percent(row.confidence)}</td>
          <td className="p-2">{percent(row.correctness)}</td>
          <td className="p-2">{row.count}</td>
        </tr>)}</tbody>
      </table>
    </div>
  </details>
}
