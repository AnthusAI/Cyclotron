import {Activity, ArrowLeft, ArrowRight, ChevronsLeftRight, CircleHelp, FileText, Maximize, PanelRight, Play, ShieldCheck, Workflow, X, ZoomIn, ZoomOut} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {Badge} from '@/components/ui/badge'
import {Card, CardContent, CardHeader, CardTitle} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {NativeSelect, NativeSelectOption} from '@/components/ui/native-select'
import {Label} from '@/components/ui/label'
import {Separator} from '@/components/ui/separator'
import {FilterCheckbox} from './FilterCheckbox'

export type Counts={predictions:number;labels:number;optimizations:number;events:number}

export function App({counts}:{counts:Counts}) {
  return <>
    <div className="app-shell">
      <header className="flex items-center justify-between gap-4 border-b border-border px-6 py-4">
        <div className="flex items-center gap-3"><div className="flex size-9 items-center justify-center rounded-xl bg-primary text-primary-foreground"><Workflow className="size-5" /></div><div><p className="text-sm font-semibold">Decision Flywheel</p><p className="text-xs text-muted-foreground">Experiment workspace</p></div></div>
        <Badge variant="outline"><ShieldCheck className="size-3" /> Private · offline</Badge>
      </header>
      <main className="explorer-main mx-auto w-full max-w-[1800px] gap-3 p-4">
        <div className="flex items-start justify-between gap-4"><div><p className="mb-2 text-xs font-medium uppercase tracking-widest text-muted-foreground">Observability / recorded history</p><h1 className="text-2xl font-semibold tracking-tight">Run explorer</h1><p className="mt-1 text-sm text-muted-foreground">Follow predictions, human feedback, and each optimization experiment.</p></div><Badge variant="secondary" className="mt-6"><Activity className="size-3" /> Recorded session</Badge></div>
        <details className="run-statistics text-muted-foreground">
          <summary className="cursor-pointer text-xs font-medium">Run statistics</summary>
          <div className="mt-3 grid grid-cols-2 gap-3 xl:grid-cols-4">
          {([['Model predictions',counts.predictions,'Before human review'],['Human feedback',counts.labels,'Votes, comments & revisions'],['Optimizer requests',counts.optimizations,'Recorded LLM exchanges'],['Runtime events',counts.events,'Immutable source recording']] as const).map(([title,value,description])=><Card key={title} className="gap-2 py-4"><CardHeader className="px-4 pb-0"><p className="text-xs text-muted-foreground">{title}</p></CardHeader><CardContent className="px-4"><p className="font-mono text-2xl font-medium tracking-tight">{value}</p><p className="mt-1 text-xs text-muted-foreground">{description}</p></CardContent></Card>)}
          </div>
        </details>
        <div id="workspace" className="workspace">
          <Card className="timeline-pane gap-0 overflow-hidden py-0">
            <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3 px-5 py-3"><div><CardTitle className="text-base">Decision timeline</CardTitle><p id="navigation-hint" className="mt-1 text-xs text-muted-foreground">Horizontal scroll: pan · vertical scroll: rows</p></div><div className="flex items-center gap-1">
              <Button id="fullscreen-toggle" variant="outline" size="icon" aria-label="Enter fullscreen" title="Enter fullscreen"><Maximize /></Button>
              <Button id="zoom-in" variant="outline" size="icon" aria-label="Zoom in" title="Zoom in"><ZoomIn /></Button>
              <Button id="zoom-out" variant="outline" size="icon" aria-label="Zoom out" title="Zoom out"><ZoomOut /></Button>
              <Button id="fit-all" variant="outline" size="sm" title="Return to a four-cycle view"><ChevronsLeftRight /> Reset zoom</Button><Button id="show-inspector" variant="secondary" size="sm" hidden><PanelRight /> Show details</Button>
            </div></CardHeader>
            <Separator />
            <div className="flex flex-wrap items-center gap-2 px-5 py-3"><Button id="show-history" variant="secondary" size="sm">Recorded review history</Button><Button id="show-run" variant="outline" size="sm">This optimization run</Button><span className="ml-auto flex items-center gap-1 text-xs text-muted-foreground"><CircleHelp className="size-3" /> Cycles → steps → events</span></div>
            <div className="flex flex-wrap items-center gap-4 border-y border-border bg-muted/30 px-5 py-3">
              <div className="flex items-center gap-2"><Label htmlFor="label-filter" className="text-xs text-muted-foreground">Class</Label><NativeSelect id="label-filter" aria-label="Label" size="sm"><NativeSelectOption value="">All labels</NativeSelectOption></NativeSelect></div>
              <div className="flex items-center gap-2"><Label htmlFor="role-filter" className="text-xs text-muted-foreground">Partition</Label><NativeSelect id="role-filter" aria-label="Partition" size="sm"><NativeSelectOption value="">All partitions</NativeSelectOption></NativeSelect></div>
              <FilterCheckbox id="comment-filter" label="With comments" /><FilterCheckbox id="disagreement-filter" label="Disagreements only" />
            </div>
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
              <section id="optimizer-exchange" className="space-y-3" hidden aria-label="Optimizer LLM exchange">
                <details id="optimizer-request-box" className="disclosure"><summary>Optimizer LLM request · full context</summary><pre id="optimizer-request-content" /></details>
                <details id="optimizer-response-box" className="disclosure"><summary>Optimizer LLM responses · tool calls</summary><pre id="optimizer-response-content" /></details>
              </section>
              <section id="decision-exchange" className="space-y-3" hidden aria-label="Decision model exchange">
                <details id="decision-request-box" className="disclosure"><summary>Exact decision-model request</summary><pre id="decision-request-content" /></details>
                <details id="decision-response-box" className="disclosure"><summary>Exact decision-model response</summary><pre id="decision-response-content" /></details>
              </section>
              <dl id="event-fields" />
              <details id="content-box" className="disclosure"><summary id="content-title">Inspect event content</summary><pre id="event-content" /></details>
              <details className="disclosure"><summary>Configuration at this point</summary><pre id="configuration" /></details>
              <details className="disclosure"><summary>Exact raw event</summary><pre id="raw-event" /></details>
            </CardContent>
          </Card>
        </div>
        <p className="text-xs text-muted-foreground">Read-only playback. No model calls, no changes to labels, no data sent outside this machine.</p>
      </main>
    </div>
  </>
}
