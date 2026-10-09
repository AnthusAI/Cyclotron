import {Activity, ArrowLeft, ArrowRight, ChevronsLeftRight, FileText, PanelRight, Play, ShieldCheck, X, ZoomIn, ZoomOut} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Badge} from '@/components/ui/badge'
import {Card, CardContent, CardHeader} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {NativeSelect, NativeSelectOption} from '@/components/ui/native-select'
import {Label} from '@/components/ui/label'
import {FilterCheckbox} from './FilterCheckbox'
import {ExchangePanel} from './ExchangePanel'
import {PlaybackCalibration} from './ReliabilityCurve'
import {PlaybackModelComparison} from './ModelComparison'
import {CyclotronBrand} from './CyclotronBrand'
import {LabMetrics} from './components/lab-metrics'
import {RunOutcomes} from './RunOutcomes'
import type {OutcomeWindow} from './recordedOutcomes'

export type Counts={predictions:number;labels:number;optimizations:number;events:number}
type Endpoint={accuracy:number|null;precision:number|null;recall:number|null}
export type RunComparison={scope:string;sample_count:number;class_counts:Record<string,number>;before:Endpoint;after:Endpoint}
export function App({counts,comparison,outcomes=[],embedded=false}:{counts:Counts;comparison?:RunComparison|null;outcomes?:OutcomeWindow[];embedded?:boolean}) {
  return <>
    <div className="app-shell">
      {!embedded&&<header className="flex items-center justify-between gap-4 border-b border-border px-6 py-4">
        <CyclotronBrand />
        <Badge variant="outline"><ShieldCheck className="size-3" /> Private · offline</Badge>
      </header>}
      <main className={`explorer-main w-full gap-1 ${embedded?'p-0':'p-2'}`}>
        {!embedded&&<div className="flex items-start justify-between gap-4"><div><p className="mb-2 text-xs font-medium uppercase tracking-widest text-muted-foreground">Observability / recorded history</p><h1 className="text-2xl font-semibold tracking-tight">Run explorer</h1><p className="mt-1 text-sm text-muted-foreground">Follow predictions, human feedback, and each optimization experiment.</p></div><Badge variant="secondary" className="mt-6"><Activity className="size-3" /> Recorded session</Badge></div>}
        {comparison||outcomes.length?<details className="run-statistics text-muted-foreground">
          <summary className="cursor-pointer text-xs font-medium"><span>Run statistics</span><span className="ml-2 font-normal">{counts.predictions} predictions · {counts.labels} labels · {counts.optimizations} optimizer calls</span></summary>
          {comparison
            ? <div className="mt-3"><LabMetrics metrics={([['Recall','recall'],['Precision','precision'],['Accuracy','accuracy']] as const).map(([name,key])=>({name,before:comparison.before[key],after:comparison.after[key]}))} /></div>
            : <RunOutcomes windows={outcomes}/>}
          {comparison&&<p className="mt-2 text-xs leading-relaxed">{comparison.scope} · {comparison.sample_count} items · {Object.entries(comparison.class_counts).map(([label,count])=>`${label}: ${count}`).join(' · ')}. Precision and recall use the configured positive class. Small class counts make these estimates uncertain; undefined means no applicable denominator.</p>}
        </details>:<p className="text-xs text-muted-foreground"><span>Run statistics</span> · {counts.predictions} predictions · {counts.labels} labels · Outcome metrics not recorded</p>}
        <div id="workspace" className="workspace">
          <Card className="timeline-pane gap-0 overflow-hidden py-0">
            <div aria-label="Timeline controls" className="timeline-controls flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border px-2 py-1">
            <div className="flex shrink-0 items-center gap-2"><div><span className="whitespace-nowrap font-heading text-sm font-semibold">Decision timeline</span><p id="navigation-hint" className="sr-only">Horizontal scroll: pan · vertical scroll: rows</p></div><div className="flex items-center gap-1">
              <Button id="zoom-in" variant="outline" size="icon" aria-label="Zoom in" title="Zoom in"><ZoomIn /></Button>
              <Button id="zoom-out" variant="outline" size="icon" aria-label="Zoom out" title="Zoom out"><ZoomOut /></Button>
              <Button id="fit-all" variant="outline" size="sm" title="Show the entire recorded run"><ChevronsLeftRight /> Fit all cycles</Button><Button id="show-inspector" variant="secondary" size="sm" hidden><PanelRight /> Show details</Button>
            </div></div>
            <div className="flex flex-wrap items-center gap-1"><Button id="show-history" variant="secondary" size="sm">Recorded review history</Button><Button id="show-run" variant="outline" size="sm">This optimization run</Button></div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="flex items-center gap-2"><Label htmlFor="label-filter" className="text-xs text-muted-foreground">Class</Label><NativeSelect id="label-filter" aria-label="Label" size="sm"><NativeSelectOption value="">All labels</NativeSelectOption></NativeSelect></div>
              <div className="flex items-center gap-2"><Label htmlFor="role-filter" className="text-xs text-muted-foreground">Partition</Label><NativeSelect id="role-filter" aria-label="Partition" size="sm"><NativeSelectOption value="">All partitions</NativeSelectOption></NativeSelect></div>
              <FilterCheckbox id="comment-filter" label="With explanations" /><FilterCheckbox id="disagreement-filter" label="Disagreements only" />
            </div></div>
            <CardContent className="timeline-viewport px-0 py-0"><div id="timeline" aria-label="Recorded decision and feedback timeline" /></CardContent>
            <div className="timeline-footer space-y-2 border-t border-border px-5 py-3"><div className="flex flex-wrap items-center justify-between gap-2"><p id="status" className="font-mono text-xs text-muted-foreground" aria-live="polite" /><div className="flex items-center gap-1"><Button id="back" variant="ghost" size="sm"><ArrowLeft /> Previous</Button><Button id="play" variant="secondary" size="sm"><Play /> Play</Button><Button id="next" variant="ghost" size="sm">Next <ArrowRight /></Button></div></div>
              <details className="recording-details text-xs text-muted-foreground"><summary className="cursor-pointer font-medium">Playback range & recording details</summary><p id="run-bounds" className="mt-3 text-xs leading-relaxed text-muted-foreground" /><div className="mt-3 flex flex-wrap items-end gap-3"><div className="space-y-1"><Label htmlFor="start">From event</Label><Input id="start" type="number" min="1" defaultValue="1" className="w-24" /></div><div className="space-y-1"><Label htmlFor="end">Through event</Label><Input id="end" type="number" min="1" className="w-24" /></div><div className="space-y-1"><Label htmlFor="round">Round</Label><NativeSelect id="round"><NativeSelectOption value="">Select a recorded step</NativeSelectOption></NativeSelect></div></div><p id="timeline-note" className="mt-3 leading-relaxed" /><p id="runtime-health" role="status" className="mt-2 font-mono">Starting viewer…</p><details className="mt-3"><summary className="cursor-pointer">Open-source licenses</summary><pre id="vendor-license" /></details></details>
            </div>
          </Card>
          <Card id="inspector" className="inspector-card gap-0 py-0">
            <CardHeader className="flex flex-row items-center justify-between gap-3 border-b border-border px-5 py-3"><p className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-muted-foreground"><FileText className="size-4" /> Event inspector</p><Button id="close-inspector" variant="ghost" size="icon" aria-label="Close details"><X /></Button></CardHeader>
            <CardContent className="inspector-body space-y-4 px-5 py-5"><div><h2 id="event-title" className="text-lg font-semibold capitalize tracking-tight">Select a timeline event</h2><p id="event-summary" className="mt-2 text-xs leading-relaxed text-muted-foreground" /></div><Button id="paired-request" variant="outline" size="sm" className="w-full" hidden>Inspect matching optimizer request</Button>
              <section id="cycle-states" className="space-y-3" hidden aria-label="Cycle before and after states">
                <p id="cycle-state-summary" className="text-xs text-muted-foreground" />
                <details className="disclosure"><summary>Before this cycle</summary><pre id="cycle-before" /></details>
                <details className="disclosure"><summary>After this cycle</summary><pre id="cycle-after" /></details>
              </section>
              <section id="evaluation-visual" className="disclosure p-3 space-y-3" hidden aria-label="Evaluation metric comparison">
                <h3 className="text-sm font-semibold">Evaluation outcome</h3><p id="evaluation-summary" className="text-xs text-muted-foreground" /><div id="evaluation-bars" />
              </section>
              <section id="optimizer-proposal" className="space-y-3" hidden aria-label="Proposed configuration and recorded outcome">
                <h3 className="text-sm font-semibold">Proposed change</h3>
                <p id="optimizer-proposal-status" className="text-sm" />
                <details className="disclosure"><summary>Before → proposed rubric / configuration</summary><pre id="optimizer-proposal-content" /></details>
              </section>
              <ExchangePanel kind="optimizer" title="Optimizer LLM" />
              <ExchangePanel kind="decision" title="Decision model" />
              <section id="cell-events" className="cycle-cell-events" hidden aria-label="Events in this cycle and row" />
              <PlaybackCalibration />
              <PlaybackModelComparison />
              <dl id="event-fields" />
              <details id="content-box" className="disclosure"><summary id="content-title">Inspect event content</summary><pre id="event-content" /></details>
              <details className="disclosure"><summary>Configuration at this point</summary><pre id="configuration" /></details>
              <details className="disclosure"><summary>Exact raw event</summary><pre id="raw-event" /></details>
            </CardContent>
          </Card>
        </div>
        {!embedded&&<p className="text-xs text-muted-foreground">Read-only playback. No model calls, no changes to labels, no data sent outside this machine.</p>}
      </main>
    </div>
  </>
}
