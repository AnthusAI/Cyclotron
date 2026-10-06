import { createRoot } from 'react-dom/client'
import {flushSync} from 'react-dom'
import './index.css'
import {App} from './App.tsx'

const read=(id:string):Record<string,unknown>[]=>JSON.parse(document.getElementById(id)?.textContent||'[]')
const events=read('recording'),history=read('review-history')
const counts={
  predictions:history.filter(e=>e.source_table==='presentations').length+events.filter(e=>e.kind==='prediction').length,
  labels:history.filter(e=>e.source_table==='review_events').length+events.filter(e=>e.kind==='human-feedback').length,
  optimizations:events.filter(e=>e.kind==='optimizer-request').length,
  events:events.length,
}
// The DOM adapter initializes after this synchronous shell mount, exactly once.
flushSync(()=>createRoot(document.getElementById('root')!).render(<App counts={counts} />))
