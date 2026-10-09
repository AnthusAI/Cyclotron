import {render,screen,cleanup} from '@testing-library/react'
import {afterEach,it,expect,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ClassifierImpact} from './ClassifierImpact'
import {graphql} from './graphql'
vi.mock('./graphql',()=>({graphql:vi.fn()}))
afterEach(()=>{cleanup();vi.clearAllMocks()})

it('reports a failed membership lookup instead of claiming no cyclotrons are affected',async()=>{
  vi.mocked(graphql).mockRejectedValue(new Error('Service unavailable'))
  render(<ClassifierImpact classifierId="shared"/> )
  expect(await screen.findByRole('alert')).toHaveTextContent('Could not load affected cyclotrons: Service unavailable')
  expect(screen.queryByText('This classifier is not in an active cyclotron definition.')).toBeNull()
  expect(vi.mocked(graphql).mock.calls.every(([q])=>!q.includes('mutation'))).toBe(true)
})
