type RecordValue = Record<string, unknown>
const record = (value: unknown): RecordValue | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as RecordValue : undefined
const text = (value: unknown): string | undefined => typeof value === 'string' && value.trim() ? value : undefined
const eventId = (value: unknown): string => typeof value === 'string' || typeof value === 'number' ? String(value) : 'Not recorded'

function SampleProvenance({sample}: {sample: RecordValue}) {
  const provenance = record(sample.calibration_provenance)
  const training = Array.isArray(provenance?.training_ids) && provenance.training_ids.every(id => typeof id === 'string')
    ? provenance.training_ids as string[] : undefined
  const trainingCount = typeof provenance?.training_count === 'number' && Number.isInteger(provenance.training_count)
    && provenance.training_count >= 0 ? provenance.training_count : undefined
  const source = sample.source === 'calibrated-head' ? 'Calibrated ML head'
    : sample.source === 'decision-passthrough' ? 'Raw decision output' : 'Output source not recorded'
  const fit = text(provenance?.fit_on)
  const temperature = typeof sample.temperature === 'number' && Number.isFinite(sample.temperature) && sample.temperature > 0
    ? sample.temperature.toFixed(3) : 'Not recorded'
  return <details className="rounded-md border bg-card p-2 text-xs">
    <summary className="cursor-pointer break-words font-medium focus-visible:outline-2 focus-visible:outline-ring">
      <span>{String(sample.item_id)}</span><span aria-hidden="true"> · </span><span className="font-normal text-muted-foreground">{source}</span>
    </summary>
    <div className="mt-2 space-y-2">
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1">
        <dt className="text-muted-foreground">Model version</dt><dd className="break-all font-mono">{text(sample.version) ?? 'Not recorded'}</dd>
        <dt className="text-muted-foreground">Prediction event</dt><dd>{eventId(sample.prediction_event_id)}</dd>
        <dt className="text-muted-foreground">Feedback event</dt><dd>{eventId(sample.feedback_event_id)}</dd>
        {provenance ? <>
          <dt className="text-muted-foreground">Calibration method</dt><dd>{text(provenance.method) ?? 'Not recorded'}</dd>
          <dt className="text-muted-foreground">Fitted on</dt><dd>{fit === 'out_of_fold' || fit === 'oof' ? 'Out-of-fold training predictions' : fit ?? 'Not recorded'}</dd>
          <dt className="text-muted-foreground">Temperature</dt><dd>{temperature}</dd>
        </> : null}
      </dl>
      {!provenance ? <p className="text-muted-foreground">{sample.source === 'decision-passthrough'
        ? 'No ML-head calibration was applied.' : 'Calibration provenance not recorded.'}</p> : null}
      {training ? <details><summary className="cursor-pointer">{training.length} trusted training items</summary>
        <ul className="mt-1 break-all font-mono">{training.map((id,index) => <li key={`${id}:${index}`}>{id}</li>)}</ul>
      </details> : trainingCount !== undefined ? <p className="text-muted-foreground">{trainingCount} trusted training items; their IDs are on the prediction event.</p>
        : provenance ? <p className="text-muted-foreground">Training item IDs not recorded.</p> : null}
    </div>
  </details>
}

/** Displays recorded snapshot evidence only; never infers a fit from a curve. */
export function CalibrationProvenance({samples, versionCounts}: {samples: unknown[]; versionCounts?: Record<string, number>}) {
  const rows = samples.flatMap(value => {
    const sample = record(value)
    return sample && text(sample.item_id) ? [sample] : []
  })
  const missing = samples.length - rows.length
  return <details className="mt-2">
    <summary className="cursor-pointer text-xs">Snapshot provenance</summary>
    <p className="my-2 text-xs text-muted-foreground">{rows.length} recorded samples{versionCounts
      ? ` · ${Object.keys(versionCounts).length} model ${Object.keys(versionCounts).length === 1 ? 'version' : 'versions'} represented` : ''}. Historical predictions, not a new fit on this window.</p>
    {missing ? <p className="mb-2 text-xs text-muted-foreground">{missing} sample records could not be interpreted.</p> : null}
    <div className="max-h-80 space-y-2 overflow-y-auto overscroll-contain focus-visible:outline-2 focus-visible:outline-ring"
      role="region" aria-label="Calibration sample provenance" tabIndex={0}>
      {rows.map(sample => <SampleProvenance key={String(sample.item_id)} sample={sample} />)}
    </div>
  </details>
}
