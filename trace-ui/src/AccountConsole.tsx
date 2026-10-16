import {useCallback,useEffect,useRef,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {CyclotronBrand} from './CyclotronBrand'
import {AccountSwitcher} from '@anthus/prospectus-accounts-client/react'
import {consoleAccounts,SignInExpired,type ConsoleAccountsClient,type ConsoleSession} from './consoleSession'
import {WebApp} from './WebApp'
import {WorkspaceTransportProvider} from './WorkspaceTransport'

export function AccountConsole({client=consoleAccounts}:{client?:ConsoleAccountsClient}){
  const [session,setSession]=useState<ConsoleSession|null>(null)
  const [busy,setBusy]=useState(false),[error,setError]=useState('')
  const [transition,setTransition]=useState<string|null>(null)
  const generation=useRef(0),active=useRef(true)
  const refreshPending=useRef<Promise<void>|null>(null),busyRef=useRef(false)
  const lastRefreshAt=useRef(0)
  const expired=useCallback(()=>{
    generation.current++
    setSession({authenticated:false,loginUrl:'/auth/login'})
    window.history.replaceState(null,'',window.location.pathname+window.location.search)
  },[])
  const refresh=useCallback(()=>{
    if(refreshPending.current)return refreshPending.current
    lastRefreshAt.current=Date.now()
    const current=++generation.current
    const pending=(async()=>{
    try{
      const state=await client.session()
      if(active.current&&current===generation.current){setSession(state);setError('')}
    }catch(cause){
      if(active.current&&current===generation.current){setSession(null);setError(cause instanceof Error?cause.message:'Account service unavailable. Try again.')}
    }
    })()
    refreshPending.current=pending
    void pending.finally(()=>{if(refreshPending.current===pending)refreshPending.current=null})
    return pending
  },[client])
  useEffect(()=>{
    let cancelled=false
    active.current=true
    void Promise.resolve().then(()=>{if(!cancelled)void refresh()})
    // Native menus can emit window focus on touch browsers. Refresh only when
    // returning from a background tab; operations still authorize independently.
    const visible=()=>{if(document.visibilityState==='visible'&&!busyRef.current&&Date.now()-lastRefreshAt.current>=30000)void refresh()}
    document.addEventListener('visibilitychange',visible)
    return ()=>{cancelled=true;active.current=false;document.removeEventListener('visibilitychange',visible)}
  },[refresh])

  const change=async(action:()=>Promise<ConsoleSession|void>,label:string)=>{
    if(busyRef.current)return
    busyRef.current=true
    setBusy(true);setTransition(label);setError('')
    const current=++generation.current
    window.history.replaceState(null,'',window.location.pathname+window.location.search)
    try{
      const result=await action()
      if(!active.current||current!==generation.current)return
      window.history.replaceState(null,'',window.location.pathname+window.location.search)
      if(result&&'authenticated' in result){setSession(result);setError('')}
      else{
        if(refreshPending.current)await refreshPending.current
        await refresh()
      }
    }catch(cause){
      if(!active.current||current!==generation.current)return
      if(cause instanceof SignInExpired)setSession({authenticated:false,loginUrl:'/auth/login'})
      else setSession(null)
      setError(cause instanceof Error?cause.message:'The account request failed. Try again.')
    }finally{busyRef.current=false;if(active.current){setBusy(false);setTransition(null)}}
  }
  const selected=session?.authenticated?session.accounts.find(account=>account.id===session.selectedAccountId):undefined
  const workspace=!!selected&&!transition
  const select=(id:string)=>{
    if(!session?.authenticated)return
    const account=session.accounts.find(value=>value.id===id)
    if(!account||id===session.selectedAccountId)return
    void change(()=>client.select(id,session.csrfToken),`Loading ${account.name} workspace…`)
  }

  return <div className="account-console flex h-dvh min-h-0 flex-col bg-background text-foreground">
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
      <CyclotronBrand/>
      {session?.authenticated?<div className="flex flex-wrap items-center gap-3">
        {session.accounts.length?<AccountSwitcher accounts={session.accounts} selectedAccountId={session.selectedAccountId} disabled={busy} onSelect={select}/>:null}
        <Button variant="outline" className="min-h-11" disabled={busy} onClick={()=>{void change(async()=>{await client.logout(session.csrfToken);return {authenticated:false,loginUrl:'/auth/login'}},'Signing out…')}}>Sign out</Button>
      </div>:null}
    </header>
    {error?<div role="alert" className="shrink-0 border-b border-destructive/30 bg-destructive/10 px-4 py-3 text-sm">{error}</div>:null}
    {!session?<main className="grid flex-1 place-items-center p-6"><div className="space-y-4 text-center">{error?<Button onClick={()=>{void refresh()}}>Try again</Button>:<p role="status">Loading your accounts…</p>}</div></main>:null}
    {session&&!session.authenticated?<main className="grid flex-1 place-items-center p-6"><Card className="w-full max-w-lg"><CardHeader><CardTitle><h1>Welcome to Cyclotron</h1></CardTitle></CardHeader><CardContent className="space-y-5"><p className="text-sm text-muted-foreground">Sign in to choose your account and open its workspace.</p><a className="inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus-visible:outline-2 focus-visible:outline-offset-2" href="/auth/login">Continue with Google</a></CardContent></Card></main>:null}
    {session?.authenticated&&transition?<main className="grid flex-1 place-items-center p-6" aria-busy="true"><p role="status" className="text-center text-sm text-muted-foreground">{transition}</p></main>:null}
    {session?.authenticated&&!selected&&!transition?<main className="flex min-h-0 flex-1 flex-col items-center overflow-y-auto p-4 sm:p-6"><Card className="my-auto w-full max-w-xl shrink-0"><CardHeader><CardTitle><h1>{session.accounts.length?'Choose an account':'No accounts available'}</h1></CardTitle></CardHeader><CardContent className="space-y-5">
      {session.accounts.length?<>
        <p className="text-sm text-muted-foreground">Choose an account to open its workspace.</p>
        <ul className="space-y-3" aria-label="Available accounts">{session.accounts.map(account=><li key={account.id}>
          <button type="button" aria-label={`Open ${account.name} workspace (${account.role})`} disabled={busy} onClick={()=>select(account.id)} className="flex min-h-16 w-full items-center justify-between gap-4 rounded-lg border border-border bg-background px-4 py-4 text-left transition-colors hover:bg-accent focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-ring disabled:opacity-50">
            <span className="min-w-0"><span className="block break-words font-medium [overflow-wrap:anywhere]">{account.name}</span><span className="mt-1 block text-xs capitalize text-muted-foreground">{account.role}</span></span>
            <span className="shrink-0 text-xs text-muted-foreground">Open workspace<span aria-hidden="true"> →</span></span>
          </button>
        </li>)}</ul>
      </>:<p className="text-sm text-muted-foreground">No accounts are available for this sign-in. Contact your account administrator if you expected access.</p>}
    </CardContent></Card></main>:null}
    {session?.authenticated&&workspace?<>
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <p>{selected.name} · {session.canWrite?'Can edit workspace':'Read-only account. Changes require write permission.'}</p>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden"><WorkspaceTransportProvider key={selected.id} accountId={selected.id} onAuthenticationRequired={expired}><WebApp embedded/></WorkspaceTransportProvider></div>
    </>:null}
  </div>
}
