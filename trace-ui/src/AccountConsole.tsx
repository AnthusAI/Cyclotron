import {useCallback,useEffect,useRef,useState} from 'react'
import {Button} from '@/components/ui/button'
import {Card,CardContent,CardHeader,CardTitle} from '@/components/ui/card'
import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {CyclotronBrand} from './CyclotronBrand'
import {AccountSwitcher} from '@anthus/prospectus-accounts-client/react'
import {consoleAccounts,SignInExpired,type ConsoleAccountsClient,type ConsoleSession} from './consoleSession'
import {WebApp} from './WebApp'
import {WorkspaceTransportProvider} from './WorkspaceTransport'

export function AccountConsole({client=consoleAccounts}:{client?:ConsoleAccountsClient}){
  const [session,setSession]=useState<ConsoleSession|null>(null)
  const [busy,setBusy]=useState(false),[error,setError]=useState(''),[name,setName]=useState('')
  const [switching,setSwitching]=useState(false)
  const [creationKey,setCreationKey]=useState(()=>crypto.randomUUID())
  const generation=useRef(0),active=useRef(true)
  const expired=useCallback(()=>{
    generation.current++
    setSession({authenticated:false,loginUrl:'/auth/login'})
    window.history.replaceState(null,'',window.location.pathname+window.location.search)
  },[])
  const refresh=useCallback(async()=>{
    const current=++generation.current
    try{
      const state=await client.session()
      if(active.current&&current===generation.current){setSession(state);setError('')}
    }catch(cause){
      if(active.current&&current===generation.current){setSession(null);setError(cause instanceof Error?cause.message:'Account service unavailable. Try again.')}
    }
  },[client])
  useEffect(()=>{
    let cancelled=false
    active.current=true
    void Promise.resolve().then(()=>{if(!cancelled)void refresh()})
    const focus=()=>{void refresh()}
    window.addEventListener('focus',focus)
    return ()=>{cancelled=true;active.current=false;window.removeEventListener('focus',focus)}
  },[refresh])

  const change=async(action:()=>Promise<void>)=>{
    setBusy(true);setSwitching(true);setError('');generation.current++
    try{
      await action()
      window.history.replaceState(null,'',window.location.pathname+window.location.search)
      await refresh()
    }catch(cause){
      if(cause instanceof SignInExpired)setSession({authenticated:false,loginUrl:'/auth/login'})
      setError(cause instanceof Error?cause.message:'The account request failed. Try again.')
    }finally{setBusy(false);setSwitching(false)}
  }
  const selected=session?.authenticated?session.accounts.find(account=>account.id===session.selectedAccountId):undefined
  const workspace=!!selected&&!switching
  const createForm=session?.authenticated?<form className="flex flex-wrap items-end gap-3" onSubmit={event=>{
    event.preventDefault()
    if(busy||!name.trim())return
    void change(async()=>{await client.create(name.trim(),creationKey,session.csrfToken);setName('');setCreationKey(crypto.randomUUID())})
  }}>
    <div className="space-y-2"><Label htmlFor="account-name">Account name</Label><Input id="account-name" value={name} maxLength={200} required disabled={busy} onChange={event=>{setName(event.target.value);setCreationKey(crypto.randomUUID())}} autoComplete="organization" placeholder="Your team or organization"/></div>
    <Button type="submit" disabled={busy||!name.trim()}>Create account</Button>
  </form>:null

  return <div className="account-console flex h-dvh min-h-0 flex-col bg-background text-foreground">
    <header className="flex shrink-0 flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
      <CyclotronBrand/>
      {session?.authenticated?<div className="flex flex-wrap items-center gap-3">
        {session.accounts.length?<AccountSwitcher accounts={session.accounts} selectedAccountId={session.selectedAccountId} disabled={busy} onSelect={id=>{void change(()=>client.select(id,session.csrfToken))}}/>:null}
        <Button variant="outline" disabled={busy} onClick={()=>{void change(async()=>{await client.logout(session.csrfToken);generation.current++;setSession({authenticated:false,loginUrl:'/auth/login'})})}}>Sign out</Button>
      </div>:null}
    </header>
    {error?<div role="alert" className="shrink-0 border-b border-destructive/30 bg-destructive/10 px-4 py-3 text-sm">{error}</div>:null}
    {!session?<main className="grid flex-1 place-items-center p-6"><div className="space-y-4 text-center">{error?<Button onClick={()=>{void refresh()}}>Try again</Button>:<p role="status">Loading your accounts…</p>}</div></main>:null}
    {session&&!session.authenticated?<main className="grid flex-1 place-items-center p-6"><Card className="w-full max-w-lg"><CardHeader><CardTitle><h1>Welcome to Cyclotron</h1></CardTitle></CardHeader><CardContent className="space-y-5"><p className="text-sm text-muted-foreground">Sign in to choose your account and open its workspace.</p><a className="inline-flex rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground focus-visible:outline-2 focus-visible:outline-offset-2" href="/auth/login">Continue with Google</a></CardContent></Card></main>:null}
    {session?.authenticated&&!workspace?<main className="grid flex-1 place-items-center overflow-auto p-6"><Card className="w-full max-w-xl"><CardHeader><CardTitle><h1>{session.accounts.length?'Choose an account':'Create your account'}</h1></CardTitle></CardHeader><CardContent className="space-y-6">
      <p className="text-sm text-muted-foreground">{switching?'Opening account…':session.accounts.length?'Use the account menu above to open a workspace, or create an account below.':'Give your account a name to get started. You can belong to more than one account.'}</p>
      {createForm}
    </CardContent></Card></main>:null}
    {session?.authenticated&&workspace?<>
      <div className="flex shrink-0 flex-wrap items-center justify-between gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
        <p>{selected.name} · {session.canWrite?'Can edit workspace':'Read-only account. Changes require write permission.'}</p>
        <details><summary className="cursor-pointer">Create another account</summary><div className="py-3">{createForm}</div></details>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden"><WorkspaceTransportProvider key={selected.id} accountId={selected.id} onAuthenticationRequired={expired}><WebApp embedded/></WorkspaceTransportProvider></div>
    </>:null}
  </div>
}
