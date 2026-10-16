import {afterEach,expect,test,vi} from 'vitest'
import {act,cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {AccountConsole} from './AccountConsole'
import {SignInExpired,type ConsoleAccountsClient,type AuthenticatedConsoleSession,type AccountSummary} from './consoleSession'

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

test('the first selection offers tappable accounts without account creation',async()=>{
  const client=clientFor()
  render(<AccountConsole client={client}/> )
  expect(await screen.findByRole('button',{name:'Open Alpha team workspace (owner)'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Open Beta team workspace (viewer)'})).toBeVisible()
  expect(screen.queryByRole('textbox',{name:'Account name'})).toBeNull()
  expect(screen.queryByText(/Create.*account/i)).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Open Alpha team workspace (owner)'}))
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
  expect(screen.queryByText(/Create.*account/i)).toBeNull()
  expect(client.create).not.toHaveBeenCalled()
})

test('no accounts shows an honest empty state without onboarding or workspace',async()=>{
  const client=clientFor({...signedIn,personId:null,accounts:[]})
  render(<AccountConsole client={client}/> )
  expect(await screen.findByRole('heading',{name:'No accounts available'})).toBeVisible()
  expect(screen.getByText('No accounts are available for this sign-in. Contact your account administrator if you expected access.')).toBeVisible()
  expect(screen.queryByRole('combobox')).toBeNull()
  expect(screen.queryByRole('textbox')).toBeNull()
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  expect(client.create).not.toHaveBeenCalled()
})

test('a tap immediately shows workspace loading and repeated taps cannot repeat selection',async()=>{
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const client=clientFor()
  vi.mocked(client.select).mockImplementation(()=>new Promise(resolve=>{complete=resolve}))
  render(<AccountConsole client={client}/> )
  const choice=await screen.findByRole('button',{name:'Open Alpha team workspace (owner)'})
  fireEvent.click(choice)
  fireEvent.click(choice)
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'beta'}})
  window.history.replaceState(null,'','#run=old-private-run')
  fireEvent(window,new PopStateEvent('popstate'))
  expect(screen.getByRole('status')).toHaveTextContent('Loading Alpha team workspace…')
  expect(screen.queryByRole('heading',{name:'Choose an account'})).toBeNull()
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  expect(screen.queryByText(/Create.*account/i)).toBeNull()
  expect(screen.getByRole('combobox',{name:'Account'})).toBeDisabled()
  expect(client.select).toHaveBeenCalledTimes(1)
  complete({...signedIn,selectedAccountId:'alpha',canWrite:true})
  expect(await screen.findByText('Existing console workspace')).toBeVisible()
  expect(window.location.hash).toBe('')
})

test('switching hides old private workspace before awaiting fresh authorization',async()=>{
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const client=clientFor({...signedIn,selectedAccountId:'alpha',canWrite:true})
  vi.mocked(client.select).mockImplementation(()=>new Promise(resolve=>{complete=resolve}))
  render(<AccountConsole client={client}/> )
  await screen.findByText('Existing console workspace')
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'beta'}})
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  expect(screen.getByRole('status')).toHaveTextContent('Loading Beta team workspace…')
  complete({...signedIn,selectedAccountId:'beta',canWrite:false})
  expect(await screen.findByText(/Read-only account/)).toBeVisible()
  expect(screen.queryByText(/Create.*account/i)).toBeNull()
})

test('reload awaits fresh session verification before mounting a selected workspace',async()=>{
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const client=clientFor()
  vi.mocked(client.session).mockImplementation(()=>new Promise(resolve=>{complete=resolve}))
  const view=render(<AccountConsole client={client}/> )
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(1))
  expect(screen.getByRole('status')).toHaveTextContent('Loading your accounts')
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  view.unmount()
  render(<AccountConsole client={client}/> )
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(2))
  complete({...signedIn,selectedAccountId:'beta',canWrite:false})
  expect(await screen.findByText(/Read-only account/)).toBeVisible()
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

test('repeated background-return events coalesce an in-flight account request',async()=>{
  const now=Date.now()
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const client=clientFor()
  vi.mocked(client.session).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve}))
  render(<AccountConsole client={client}/> )
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(1))
  vi.spyOn(Date,'now').mockReturnValue(now+60000)
  vi.spyOn(document,'visibilityState','get').mockReturnValue('visible')
  fireEvent(document,new Event('visibilitychange'))
  fireEvent(document,new Event('visibilitychange'))
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

test('opening a native account menu does not refresh a recently loaded session',async()=>{
  const client=clientFor()
  render(<AccountConsole client={client}/> )
  await screen.findByRole('heading',{name:'Choose an account'})
  fireEvent.focus(window)
  fireEvent.focus(window)
  expect(client.session).toHaveBeenCalledTimes(1)
})

test('native menu window focus never starts account loading but returning from background does',async()=>{
  const client=clientFor()
  const now=Date.now()
  render(<AccountConsole client={client}/> )
  await screen.findByRole('heading',{name:'Choose an account'})
  vi.spyOn(Date,'now').mockReturnValue(now+60000)
  vi.spyOn(document,'visibilityState','get').mockReturnValue('visible')
  fireEvent.focus(window)
  expect(client.session).toHaveBeenCalledTimes(1)
  fireEvent(document,new Event('visibilitychange'))
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(2))
})


test('a denied first selection stays outside all workspaces and retries verified state',async()=>{
  const client=clientFor()
  vi.mocked(client.select).mockRejectedValue(new Error('Account access denied'))
  render(<AccountConsole client={client}/> )
  fireEvent.click(await screen.findByRole('button',{name:'Open Alpha team workspace (owner)'}))
  expect(await screen.findByRole('alert')).toHaveTextContent('Account access denied')
  expect(screen.queryByText('Existing console workspace')).toBeNull()
  fireEvent.click(screen.getByRole('button',{name:'Try again'}))
  expect(await screen.findByRole('heading',{name:'Choose an account'})).toBeVisible()
  expect(client.select).toHaveBeenCalledTimes(1)
})

test('expired authorization during selection returns to Google sign-in without workspace data',async()=>{
  const client=clientFor({...signedIn,selectedAccountId:'alpha',canWrite:true})
  vi.mocked(client.select).mockRejectedValue(new SignInExpired('Sign-in expired. Sign in again.'))
  render(<AccountConsole client={client}/> )
  await screen.findByText('Existing console workspace')
  fireEvent.change(screen.getByRole('combobox',{name:'Account'}),{target:{value:'beta'}})
  expect(await screen.findByRole('link',{name:'Continue with Google'})).toBeVisible()
  expect(screen.queryByText('Existing console workspace')).toBeNull()
})

test('an older background session result cannot replace a newly authorized selection',async()=>{
  let complete!:(state:AuthenticatedConsoleSession)=>void
  const now=Date.now(),client=clientFor()
  render(<AccountConsole client={client}/> )
  await screen.findByRole('heading',{name:'Choose an account'})
  vi.mocked(client.session).mockImplementationOnce(()=>new Promise(resolve=>{complete=resolve}))
  vi.spyOn(Date,'now').mockReturnValue(now+60000)
  vi.spyOn(document,'visibilityState','get').mockReturnValue('visible')
  fireEvent(document,new Event('visibilitychange'))
  await waitFor(()=>expect(client.session).toHaveBeenCalledTimes(2))
  vi.mocked(client.select).mockResolvedValue({...signedIn,selectedAccountId:'beta',canWrite:false})
  fireEvent.click(screen.getByRole('button',{name:'Open Beta team workspace (viewer)'}))
  expect(await screen.findByText(/Read-only account/)).toBeVisible()
  await act(async()=>complete({...signedIn,selectedAccountId:'alpha',canWrite:true}))
  expect(screen.queryByText('Can edit workspace')).toBeNull()
  expect(screen.getByRole('combobox',{name:'Account'})).toHaveValue('beta')
})
