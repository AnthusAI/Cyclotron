import {afterEach,expect,test,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {AccountConsole} from './AccountConsole'
import type {ConsoleAccountsClient,AuthenticatedConsoleSession,AccountSummary} from './consoleSession'

vi.mock('./WebApp',()=>({WebApp:()=> <div>Existing console workspace</div>}))
afterEach(()=>{cleanup();window.history.replaceState(null,'','/');vi.restoreAllMocks()})
const alpha:AccountSummary={id:'alpha',name:'Alpha team',role:'owner',entitlement:{accountId:'alpha',product:'cyclotron',status:'active',capabilities:['console:read','console:write']}}
const beta:AccountSummary={...alpha,id:'beta',name:'Beta team',role:'viewer',entitlement:{...alpha.entitlement,accountId:'beta',capabilities:['console:read']}}
const signedIn:AuthenticatedConsoleSession={authenticated:true,personId:'person',csrfToken:'csrf',accounts:[alpha,beta],selectedAccountId:null,canWrite:false}
function clientFor(initial:AuthenticatedConsoleSession=signedIn){
  let state={...initial}
  const client:ConsoleAccountsClient={
    session:vi.fn(async()=>state),
    select:vi.fn(async id=>{state={...state,selectedAccountId:id,canWrite:id==='alpha'}}),
    create:vi.fn(async()=>{state={...state,selectedAccountId:'alpha',canWrite:true}}),
    logout:vi.fn(async()=>{}),
  }
  return client
}

test('signed out people see Google sign-in and cannot mount the existing workspace',async()=>{
  const client={...clientFor(),session:vi.fn(async()=>({authenticated:false as const,loginUrl:'/auth/login'}))}
  render(<AccountConsole client={client}/> )
  expect(await screen.findByRole('link',{name:'Continue with Google'})).toHaveAttribute('href','/auth/login')
  expect(screen.queryByText('Existing console workspace')).toBeNull()
})

test('selecting a central account mounts the existing workspace and switching clears the old run route',async()=>{
  const client=clientFor()
  render(<AccountConsole client={client}/> )
  await screen.findByRole('heading',{name:'Choose an account'})
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'alpha'}})
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
  window.history.replaceState(null,'','#run=private-alpha-run')
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'beta'}})
  await waitFor(()=>expect(client.select).toHaveBeenLastCalledWith('beta','csrf'))
  expect(await screen.findByText(/Read-only account/)).toBeVisible()
  expect(window.location.hash).toBe('')
})

test('a failed create preserves its idempotency key for the same user retry',async()=>{
  const client=clientFor({...signedIn,personId:null,accounts:[]})
  vi.mocked(client.create).mockRejectedValueOnce(new Error('Service unavailable'))
  render(<AccountConsole client={client}/> )
  fireEvent.change(await screen.findByRole('textbox',{name:'Account name'}),{target:{value:'New team'}})
  fireEvent.click(screen.getByRole('button',{name:'Create account'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Service unavailable')
  fireEvent.click(screen.getByRole('button',{name:'Create account'}))
  await waitFor(()=>expect(client.create).toHaveBeenCalledTimes(2))
  const calls=vi.mocked(client.create).mock.calls
  expect(calls[0]).toEqual(calls[1])
  expect(calls[0][0]).toBe('New team')
  expect(calls[0][1]).toMatch(/^[0-9a-f-]{36}$/)
  expect(calls[0][2]).toBe('csrf')
})

test('a central failure hides the workspace and offers a retry',async()=>{
  const client=clientFor({...signedIn,selectedAccountId:'alpha',canWrite:true})
  vi.mocked(client.session).mockRejectedValueOnce(new Error('Account service unavailable'))
  render(<AccountConsole client={client}/> )
  expect(await screen.findByRole('alert')).toHaveTextContent('Account service unavailable')
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Try again'}))
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
})

test('signing out unmounts account data without storing bearer tokens in the browser',async()=>{
  const client=clientFor({...signedIn,selectedAccountId:'alpha',canWrite:true})
  vi.mocked(client.logout).mockImplementation(async()=>{vi.mocked(client.session).mockResolvedValue({authenticated:false,loginUrl:'/auth/login'})})
  render(<AccountConsole client={client}/> )
  await screen.findByText('Existing console workspace')
  fireEvent.click(screen.getByRole('button',{name:'Sign out'}))
  await waitFor(()=>expect(client.logout).toHaveBeenCalledWith('csrf'))
  expect(await screen.findByRole('link',{name:'Continue with Google'})).toBeVisible()
  expect(screen.queryByText('Existing console workspace')).toBeNull()
})

test('repeated window focus coalesces an in-flight account request',async()=>{
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const client=clientFor()
  vi.mocked(client.session).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve}))
  render(<AccountConsole client={client}/> )
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(1))
  fireEvent.focus(window)
  fireEvent.focus(window)
  expect(client.session).toHaveBeenCalledTimes(1)
  complete(signedIn)
  expect(await screen.findByRole('heading',{name:'Choose an account'})).toBeVisible()
})

test('account switching consumes the verified response without a duplicate session request',async()=>{
  const client=clientFor()
  vi.mocked(client.select).mockResolvedValue({...signedIn,selectedAccountId:'alpha',canWrite:true})
  render(<AccountConsole client={client}/> )
  await screen.findByRole('heading',{name:'Choose an account'})
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'alpha'}})
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
  expect(client.session).toHaveBeenCalledTimes(1)
})

test('an uncertain switch hides the old workspace and retries state without repeating the mutation',async()=>{
  const client=clientFor({...signedIn,selectedAccountId:'alpha',canWrite:true})
  vi.mocked(client.select).mockRejectedValue(new Error('Account service took too long. Try again.'))
  render(<AccountConsole client={client}/> )
  await screen.findByText('Existing console workspace')
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'beta'}})
  expect(await screen.findByRole('alert')).toHaveTextContent('took too long')
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Try again'}))
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
  expect(client.select).toHaveBeenCalledTimes(1)
})
