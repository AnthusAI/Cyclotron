import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ComparisonResume} from './ComparisonResume'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})
test('resume requires renewed approval and failed-call retries are separately explicit',async()=>{
  vi.mocked(graphql).mockResolvedValue({resumeMatchedComparison:{id:'queued'}})
  render(<ComparisonResume runId="comparison" ceiling={8} busy={false} enabled status="failed" />)
  expect(screen.getByRole('button',{name:'Resume comparison'})).toBeDisabled()
  fireEvent.change(screen.getByLabelText('Total request ceiling'),{target:{value:'9'}})
  fireEvent.click(screen.getByLabelText('Allow retry of failed or interrupted model calls'))
  fireEvent.click(screen.getByLabelText('Authorize paid resume requests'))
  fireEvent.click(screen.getByRole('button',{name:'Resume comparison'}))
  await screen.findByText(/Resume queued/)
  expect(vi.mocked(graphql).mock.calls[0][1]).toMatchObject({run:'comparison',ceiling:9,retryFailed:true,confirmed:true})
  expect(screen.getByRole('button',{name:'Resume comparison'})).toBeDisabled()
})
test('an unconfirmed submission reuses its request identity rather than creating duplicate work',async()=>{
  vi.mocked(graphql).mockRejectedValueOnce(new Error('Acknowledgement unavailable')).mockResolvedValueOnce({resumeMatchedComparison:{id:'queued'}})
  render(<ComparisonResume runId="comparison" ceiling={8} busy={false} enabled status="interrupted" />)
  fireEvent.click(screen.getByLabelText('Authorize paid resume requests'))
  fireEvent.click(screen.getByRole('button',{name:'Resume comparison'}))
  await screen.findByRole('alert')
  fireEvent.click(screen.getByRole('button',{name:'Resume comparison'}))
  await waitFor(()=>expect(vi.mocked(graphql).mock.calls).toHaveLength(2))
  expect(vi.mocked(graphql).mock.calls[0][1]).toEqual(vi.mocked(graphql).mock.calls[1][1])
  expect(vi.mocked(graphql).mock.calls[0][1]).toMatchObject({retryFailed:false})
})
