/** Same-origin console BFF contract. No provider tokens are exposed to this client. */
import type {AccountSummary} from '@anthus/prospectus-accounts-client'
export type {AccountSummary} from '@anthus/prospectus-accounts-client'
export type AuthenticatedConsoleSession={authenticated:true;csrfToken:string;personId:string|null;accounts:AccountSummary[];selectedAccountId:string|null;canWrite:boolean}
export type ConsoleSession=AuthenticatedConsoleSession|{authenticated:false;loginUrl:string}
export type ConsoleAccountsClient={
  session:()=>Promise<ConsoleSession>
  select:(accountId:string,csrf:string)=>Promise<ConsoleSession|void>
  create:(name:string,idempotencyKey:string,csrf:string)=>Promise<void>
  logout:(csrf:string)=>Promise<void>
}
export class SignInExpired extends Error{}
async function request<T>(path:string,options:RequestInit={}):Promise<T>{
  // No client-side give-up timer: a slow answer is still the answer, and the gateway ends a stuck request itself.
  const started=performance.now()
  let status:number|'unavailable'='unavailable'
  try{
  const response=await fetch(path,{credentials:'same-origin',cache:'no-store',...options})
  status=response.status
  if(response.status===401)throw new SignInExpired('Sign-in expired. Sign in again.')
  const body=await response.json().catch(()=>({}))
  if(!response.ok)throw new Error(typeof body.detail==='string'?body.detail:'The account request failed. Try again.')
  return body as T
  }finally{
    console.debug('cyclotron-account-request '+JSON.stringify({path,status,durationMs:Math.round(performance.now()-started)}))
  }
}
const post=<T=void>(path:string,csrf:string,body:Record<string,unknown>={},headers:Record<string,string>={})=>request<T>(path,{method:'POST',headers:{'Content-Type':'application/json','X-CSRF-Token':csrf,...headers},body:JSON.stringify(body)})
export const consoleAccounts:ConsoleAccountsClient={
  session:()=>request<ConsoleSession>('/api/session'),
  select:(accountId,csrf)=>post<ConsoleSession>('/api/accounts/select',csrf,{accountId}),
  create:(name,key,csrf)=>post('/api/accounts',csrf,{name},{'Idempotency-Key':key}),
  logout:csrf=>post('/auth/logout',csrf),
}
