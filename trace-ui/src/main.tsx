import { createRoot } from 'react-dom/client'
import {flushSync} from 'react-dom'
import './index.css'
import {App, type RunComparison} from './App.tsx'
import {Circle, CirclePlus, CircleMinus, CirclePlay} from 'lucide-react'
import {recordedOutcomeWindows} from './recordedOutcomes'

const read=(id:string):Record<string,unknown>[]=>JSON.parse(document.getElementById(id)?.textContent||'[]')
const events=read('recording'),history=read('review-history')
const counts={
  predictions:history.filter(e=>e.source_table==='presentations').length+events.filter(e=>e.kind==='prediction').length,
  labels:history.filter(e=>e.source_table==='review_events').length+events.filter(e=>e.kind==='human-feedback').length,
  optimizations:events.filter(e=>e.kind==='optimizer-request').length,
  events:events.length,
}
// The DOM adapter initializes after this synchronous shell mount, exactly once.
const comparison=JSON.parse(document.getElementById('run-comparison')?.textContent||'null') as RunComparison|null
const options=JSON.parse(document.getElementById('workspace-options')?.textContent||'{}')
const outcomes=recordedOutcomeWindows(events,JSON.parse(document.getElementById('class-config')?.textContent||'[]'))
flushSync(()=>createRoot(document.getElementById('root')!).render(<App counts={counts} comparison={comparison} outcomes={outcomes} embedded={options.embedded===true} />))
const iconHost=document.createElement('div');iconHost.hidden=true;document.body.append(iconHost)
flushSync(()=>createRoot(iconHost).render(<>
  <span id="icon-circle"><Circle strokeWidth={3} /></span>
  <span id="icon-circle-plus"><CirclePlus strokeWidth={3} /></span>
  <span id="icon-circle-minus"><CircleMinus strokeWidth={3} /></span>
  <span id="icon-circle-play"><CirclePlay strokeWidth={3} /></span>
</>))
