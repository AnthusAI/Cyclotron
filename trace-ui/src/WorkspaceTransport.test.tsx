import {useEffect} from 'react'
import {afterEach,expect,test,vi} from 'vitest'
import {cleanup,render} from '@testing-library/react'
import {WorkspaceTransportProvider} from './WorkspaceTransport'
import {useWorkspaceTransport} from './workspaceTransportContext'

afterEach(()=>{cleanup();vi.unstubAllGlobals()})
function Capture({receive}:{receive:(value:ReturnType<typeof useWorkspaceTransport>)=>void}){
  const transport=useWorkspaceTransport()
  useEffect(()=>receive(transport),[receive,transport])
  return null
}

test('old callbacks stay bound to their account after a different workspace mounts',async()=>{
  const fetcher=vi.fn(async(_input:RequestInfo|URL,_options?:RequestInit)=>new Response(JSON.stringify({data:{runs:[]}}),{status:200}))
  vi.stubGlobal('fetch',fetcher)
  let current:ReturnType<typeof useWorkspaceTransport>|undefined
  const receive=(value:ReturnType<typeof useWorkspaceTransport>)=>{current=value}
  const page=render(<WorkspaceTransportProvider accountId="alpha"><Capture receive={receive}/></WorkspaceTransportProvider>)
  const alpha=current!
  page.rerender(<WorkspaceTransportProvider accountId="beta"><Capture receive={receive}/></WorkspaceTransportProvider>)
  await alpha.graphql('mutation {saveItemList(identifier:"delayed",name:"Alpha")}')
  await current!.graphql('{runs{id}}')
  expect(fetcher.mock.calls.map(call=>call[0])).toEqual(['/graphql?account=alpha','/graphql?account=beta'])
  expect(alpha.url('/runs/run-a/timeline?revision=1')).toBe('/runs/run-a/timeline?revision=1&account=alpha')
})

test('subscription connections keep the mounted account in their URL',()=>{
  const urls:string[]=[]
  class Socket{
    static OPEN=1
    readyState=1
    constructor(url:string){urls.push(url)}
    send(){}
    close(){}
  }
  vi.stubGlobal('WebSocket',Socket)
  let client:ReturnType<typeof useWorkspaceTransport>|undefined
  render(<WorkspaceTransportProvider accountId="alpha"><Capture receive={value=>{client=value}}/></WorkspaceTransportProvider>)
  const stop=client!.subscribeEvents('run-a',0,()=>{},()=>{})
  expect(urls[0]).toContain('/graphql?account=alpha')
  stop()
})

test('an expired HTTP session tells the host to unmount private account data',async()=>{
  vi.stubGlobal('fetch',vi.fn(async()=>new Response('{}',{status:401})))
  const expired=vi.fn()
  let client:ReturnType<typeof useWorkspaceTransport>|undefined
  const page=render(<WorkspaceTransportProvider accountId="alpha" onAuthenticationRequired={expired}><Capture receive={value=>{client=value}}/></WorkspaceTransportProvider>)
  await expect(client!.graphql('{runs{id}}')).rejects.toThrow('Authentication required')
  expect(expired).toHaveBeenCalledTimes(1)
  const old=client!
  page.unmount()
  await expect(old.graphql('{runs{id}}')).rejects.toThrow('Authentication required')
  expect(expired).toHaveBeenCalledTimes(1)
})
