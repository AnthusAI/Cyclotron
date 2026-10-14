import type {ClassifierRef,PrimingClient,PrimingContract,PrimingDraft} from './components/priming-panel'
export type PrimingGraphQL=<T>(query:string,variables:Record<string,unknown>)=>Promise<T>
/** Bind the shared panel to an application's existing authenticated GraphQL transport. */
export function createPrimingClient(request:PrimingGraphQL):PrimingClient{
 return {
  contract:async(listId:string,classifiers:ClassifierRef[])=>(await request<{primingContract:PrimingContract}>('query($list:ID!,$refs:JSON!){primingContract(listId:$list,classifiers:$refs)}',{list:listId,refs:classifiers})).primingContract,
  template:async(listId,classifiers,format)=>(await request<{primingTemplate:{content:string;encoding:string}}>('query($list:ID!,$refs:JSON!,$format:String!){primingTemplate(listId:$list,classifiers:$refs,format:$format)}',{list:listId,refs:classifiers,format})).primingTemplate,
  get:async(identifier)=>(await request<{primingDraft:PrimingDraft}>('query($id:String!){primingDraft(identifier:$id)}',{id:identifier})).primingDraft,
  command:async(action,payload)=>(await request<{primingCommand:PrimingDraft}>('mutation($action:String!,$payload:JSON!){primingCommand(action:$action,payload:$payload)}',{action,payload})).primingCommand,
 }
}
