import {afterEach, expect, test} from 'vitest'
import {cleanup, fireEvent, render, screen} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {App} from './App'

afterEach(cleanup)

test('the explorer exposes accessible timeline controls without a redundant legend',()=>{
  render(<App counts={{predictions:135,labels:132,optimizations:1,events:503}} />)
  expect(screen.getByRole('button',{name:'Zoom in'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Reset zoom'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Zoom in'})).toHaveAttribute('title','Zoom in')
  expect(screen.getByRole('button',{name:'Close details'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Enter fullscreen'})).toBeVisible()
  expect(screen.getByText('Horizontal scroll: pan · vertical scroll: rows')).toBeVisible()
  expect(screen.getByText('135')).not.toBeVisible()
  expect(document.getElementById('label-legend')).toBeNull()
  expect(document.getElementById('timeline')).toBeInTheDocument()
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
