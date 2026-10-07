import {afterEach, expect, test} from 'vitest'
import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {App} from './App'

afterEach(cleanup)

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
  render(<App counts={{predictions:135,labels:132,optimizations:1,events:503}} />)
  expect(screen.getByRole('button',{name:'Zoom in'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Reset zoom'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Zoom in'})).toHaveAttribute('title','Zoom in')
  expect(screen.getByRole('button',{name:'Close details'})).toBeVisible()
  expect(screen.queryByRole('button',{name:'Enter fullscreen'})).toBeNull()
  expect(screen.getByText('Horizontal scroll: pan · vertical scroll: rows')).toBeVisible()
  expect(screen.getByText('135')).not.toBeVisible()
  expect(document.getElementById('label-legend')).toBeNull()
  expect(screen.queryByText('Cycles → steps → events')).toBeNull()
  expect(document.getElementById('timeline')).toBeInTheDocument()
  expect(document.querySelector('header .lucide-refresh-cw')).toBeNull()
  expect(document.querySelector('header svg[viewBox="0 0 72 24"]')).toBeInTheDocument()
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
  render(<App counts={{predictions:135,labels:132,optimizations:1,events:503}} />)
  const summary=screen.getByText('Run statistics')
  expect(summary.closest('details')).not.toHaveAttribute('open')
  expect(screen.queryByRole('button',{name:'Toggle color theme'})).toBeNull()
  fireEvent.click(summary)
  // Native details owns disclosure state, without re-rendering the trace controller.
  expect(summary.tagName).toBe('SUMMARY')
})

test('the embedded explorer keeps timeline controls but does not repeat the app header',()=>{
  render(<App embedded counts={{predictions:87,labels:87,optimizations:11,events:4772}} />)
  expect(screen.queryByText('Private · offline')).toBeNull()
  expect(screen.queryByText('Run explorer')).toBeNull()
  expect(screen.getByText('Run statistics')).toBeInTheDocument()
  expect(screen.getByRole('button',{name:'Zoom in'})).toBeVisible()
})
