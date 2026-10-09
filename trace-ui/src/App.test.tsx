import {afterEach, expect, test} from 'vitest'
import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {App} from './App'

afterEach(cleanup)
const first={cycle:1,count:1,probabilityCount:1,recall:0,precision:null,accuracy:0,ece:.8,brier:.6}
const outcomes=[{id:'a',classes:[],first,latest:{...first,cycle:85,count:85,accuracy:.8}}]

test('collapsed run statistics show the summary counts on the disclosure line',()=>{
  render(<App outcomes={outcomes} counts={{predictions:85,labels:80,optimizations:12,events:400}} />)
  const summary=screen.getByText('Run statistics').closest('summary')!
  expect(summary).toHaveTextContent('85 predictions')
  expect(summary).toHaveTextContent('80 labels')
  expect(summary).toHaveTextContent('12 optimizer calls')
  expect(summary.closest('details')).not.toHaveAttribute('open')
})

test('the embedded explorer uses its whole frame without a second outer gutter or width cap',()=>{
  render(<App embedded counts={{predictions:0,labels:0,optimizations:0,events:0}} />)
  const main=document.querySelector('main')!
  expect(main).toHaveClass('p-0')
  expect(main.className).not.toContain('max-w-')
  expect(document.querySelector('[aria-label="Timeline controls"]')).toContainElement(screen.getByRole('button',{name:'Zoom in'}))
  expect(document.querySelector('[aria-label="Timeline controls"]')).toContainElement(screen.getByRole('combobox',{name:'Partition'}))
})

test('expanded statistics lead with matched before and after metrics and undefined precision is not zero',()=>{
  render(<App counts={{predictions:87,labels:87,optimizations:11,events:2000}} comparison={{scope:'Matched protected audit',sample_count:18,class_counts:{include:2,exclude:16},before:{accuracy:.5,precision:null,recall:0},after:{accuracy:.75,precision:.5,recall:1}}} />)
  expect(screen.getByText('Accuracy')).toBeInTheDocument()
  expect(screen.getByText('Precision')).toBeInTheDocument()
  expect(screen.getByText('Recall')).toBeInTheDocument()
  expect(screen.getByText('75.0%')).toBeInTheDocument()
  expect(screen.getByText('+25.0 pp')).toBeInTheDocument()
  expect(screen.getByText('Undefined')).toBeInTheDocument()
  expect(screen.getByText(/include: 2/)).toBeInTheDocument()
})

test('the explorer exposes accessible timeline controls without a redundant legend',()=>{
  render(<App outcomes={outcomes} counts={{predictions:135,labels:132,optimizations:1,events:503}} />)
  expect(screen.getByRole('button',{name:'Zoom in'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Fit all cycles'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Fit all cycles'})).toHaveAttribute('title','Show the entire recorded run')
  expect(screen.getByRole('button',{name:'Zoom in'})).toHaveAttribute('title','Zoom in')
  expect(screen.getByRole('button',{name:'Close details'})).toBeVisible()
  expect(screen.queryByRole('button',{name:'Enter fullscreen'})).toBeNull()
  expect(screen.getByText('Horizontal scroll: pan · vertical scroll: rows')).toBeVisible()
  expect(screen.getByText(/135 predictions/)).toBeVisible()
  expect(document.getElementById('label-legend')).toBeNull()
  expect(screen.queryByText('Cycles → steps → events')).toBeNull()
  expect(document.getElementById('timeline')).toBeInTheDocument()
  expect(document.querySelector('header .lucide-refresh-cw')).toBeNull()
  expect(document.querySelector('header svg[viewBox="0 0 42 24"]')).toBeInTheDocument()
})

test('a shadcn checkbox notifies the existing offline filter controller',()=>{
  render(<App counts={{predictions:0,labels:0,optimizations:0,events:0}} />)
  const checkbox=document.getElementById('comment-filter') as HTMLElement & {checked?:boolean}
  let notified=false
  checkbox.onchange=()=>{notified=Boolean(checkbox.checked)}
  fireEvent.click(checkbox)
  expect(notified).toBe(true)
})

test('run statistics are collapsed initially and there is no theme control',()=>{
  render(<App outcomes={outcomes} counts={{predictions:135,labels:132,optimizations:1,events:503}} />)
  const summary=screen.getByText('Run statistics').closest('summary')!
  expect(summary.closest('details')).not.toHaveAttribute('open')
  expect(screen.queryByRole('button',{name:'Toggle color theme'})).toBeNull()
  fireEvent.click(summary)
  // Native details owns disclosure state, without re-rendering the trace controller.
  expect(summary.tagName).toBe('SUMMARY')
})

test('counts alone do not offer an empty outcome disclosure',()=>{
  render(<App counts={{predictions:85,labels:85,optimizations:12,events:400}} />)
  expect(document.querySelector('details.run-statistics')).toBeNull()
  expect(screen.getByText(/Outcome metrics not recorded/)).toBeVisible()
})

test('the embedded explorer keeps timeline controls but does not repeat the app header',()=>{
  render(<App embedded counts={{predictions:87,labels:87,optimizations:11,events:4772}} />)
  expect(screen.queryByText('Private · offline')).toBeNull()
  expect(screen.queryByText('Run explorer')).toBeNull()
  expect(screen.getByText('Run statistics')).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'Zoom in'})).toBeVisible()
})
