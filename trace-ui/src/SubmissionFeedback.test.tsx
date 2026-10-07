import {cleanup,render,screen} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {SubmissionFeedback} from './SubmissionFeedback'
afterEach(cleanup)

test('distinguishes sending, queueing, recording, and preparing the next item',()=>{
  const submission={runId:'run',count:3,jobId:'vote'}
  const {rerender}=render(<SubmissionFeedback submission={{runId:'run',count:3}} jobs={[]} />)
  expect(screen.getByRole('status')).toHaveTextContent('Sending 3 labels…')
  rerender(<SubmissionFeedback submission={submission} jobs={[{id:'vote',kind:'label',status:'pending'}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('3 labels received · waiting to record')
  rerender(<SubmissionFeedback submission={submission} jobs={[{id:'vote',kind:'label',status:'running'}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('Recording labels and updating the flywheel…')
  rerender(<SubmissionFeedback submission={submission} jobs={[{id:'vote',kind:'label',status:'completed'},{id:'next',kind:'prepare',status:'running'}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('3 labels recorded · preparing next item…')
  rerender(<SubmissionFeedback submission={submission} jobs={[{id:'vote',kind:'label',status:'completed'},{id:'next',kind:'prepare',status:'completed'}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('3 labels recorded')
  expect(screen.queryByText(/preparing next item/)).toBeNull()
})

test('a failed or unconfirmed submission never claims that labels were saved',()=>{
  const {rerender}=render(<SubmissionFeedback submission={{runId:'run',count:3,jobId:'vote'}} jobs={[{id:'vote',kind:'label',status:'failed'}]} />)
  expect(screen.getByRole('alert')).toHaveTextContent('Label submission needs attention')
  expect(screen.queryByText(/3 labels recorded/)).toBeNull()
  rerender(<SubmissionFeedback submission={{runId:'run',count:3,unconfirmed:true}} jobs={[]} />)
  expect(screen.getByRole('alert')).toHaveTextContent('could not be confirmed')
})

test('saved feedback with an optimization warning remains a successful label submission',()=>{
  render(<SubmissionFeedback submission={{runId:'run',count:3,jobId:'vote'}} jobs={[{id:'vote',kind:'label',status:'completed',result:{optimization_warnings:[{reason:'optimizer call limit reached; labeling can continue'}]}}]} />)
  expect(screen.getByRole('status')).toHaveTextContent('3 labels recorded')
  expect(screen.getByText(/Optimization paused:/)).toHaveTextContent('Your labels are saved')
  expect(screen.queryByRole('alert')).toBeNull()
})
