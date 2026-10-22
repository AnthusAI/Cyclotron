import {render,screen,fireEvent,waitFor,cleanup} from '@testing-library/react'
import {afterEach,expect,it,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {PrimingPanel,type PrimingClient,type PrimingDraft,type PrimingContract} from './priming-panel'
afterEach(cleanup)
const contract:PrimingContract={list_id:'examples',schema:{type:'object'},classifiers:[{id:'editor',revision:1,name:'Editorial',config:{question:'Publish?',classes:[{label:'publish'},{label:'not publish'}]}}],transformers:[{id:'papyrus',name:'Papyrus curation',fields:[{name:'url',label:'Source URL',required:true}]}]}
const preview:PrimingDraft={id:'draft',version:1,status:'preview',contract,source:{kind:'transform'},rows:[{row:1,item:{id:'synthetic',values:{text:'<script>Synthetic untrusted content</script>'}},labels:{},model_representation:{editor:{text:'Synthetic decision content'}}}],errors:[],fields:[]}
function client():PrimingClient{return {contract:vi.fn().mockResolvedValue(contract),template:vi.fn(),get:vi.fn().mockResolvedValue(preview),command:vi.fn().mockResolvedValue(preview)}}
it('requires an explicit label with optional rationale after showing the exact model item and keeps feedback separate',async()=>{
 const api=client();render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('button',{name:'From reference / URL'}))
 fireEvent.change(screen.getByLabelText('Source URL'),{target:{value:'https://example.invalid'}})
 fireEvent.click(screen.getByRole('button',{name:'Start curation'}))
 expect(await screen.findByText('<script>Synthetic untrusted content</script>')).toBeVisible()
 fireEvent.click(screen.getByText('Exact details used to make suggestions'))
 expect(screen.getByText(/Synthetic decision content/)).toBeVisible()
 expect(screen.queryByText(/Predicted/)).toBeNull()
 const accept=screen.getByRole('button',{name:'Add references'});expect(accept).toBeDisabled()
 fireEvent.click(screen.getByRole('radio',{name:'Editorial: publish'}))
 expect(accept).toBeEnabled()
 expect(screen.getByLabelText('Explanation for Editorial')).not.toBeRequired()
 fireEvent.change(screen.getByLabelText('Explanation for Editorial'),{target:{value:'Explicit human reason'}})
 expect(accept).toBeEnabled()
 fireEvent.change(screen.getByLabelText('Transformation feedback'),{target:{value:'Wrong abstract'}})
 fireEvent.click(screen.getByRole('button',{name:'Reject preview and revise'}))
 await waitFor(()=>expect(api.command).toHaveBeenCalledWith('feedback',expect.objectContaining({reason:'Wrong abstract'})))
 const call=vi.mocked(api.command).mock.calls.find(([action])=>action==='feedback')!
 expect(call[1]).not.toHaveProperty('labels')
})
it('speaks of adding references, without engine terms',async()=>{
 render(<PrimingPanel client={client()} listId="examples" classifiers={[{id:'editor',revision:1}]} pollInterval={0}/> )
 expect(await screen.findByRole('heading',{name:'Add references'})).toBeVisible()
 expect(screen.getByRole('region',{name:'Add references'})).not.toHaveTextContent(/priming|cyclotron|classifier|calibration|onboarding|version|sweep/i)
})
it('shows row errors and keeps acceptance unavailable',async()=>{
 const api=client();api.get=vi.fn().mockResolvedValue({...preview,status:'invalid',rows:[],errors:[{row:3,field:'editor.label',message:'Choose a configured label'}]})
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} initialDraftId="draft" pollInterval={0}/> )
 expect(await screen.findByText(/Row 3.*editor.label.*Choose a configured label/)).toBeVisible()
 expect(screen.queryByRole('button',{name:'Add references'})).toBeNull()
})
it('cancels an in-flight start and discards its late preview',async()=>{
 let resolve!:(draft:PrimingDraft)=>void
 const api=client();api.command=vi.fn().mockImplementation(action=>action==='transform'?new Promise<PrimingDraft>(done=>resolve=done):Promise.resolve({...preview,status:'cancelled',rows:[]}))
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('button',{name:'From reference / URL'}));fireEvent.change(screen.getByLabelText('Source URL'),{target:{value:'https://example.invalid'}});fireEvent.click(screen.getByRole('button',{name:'Start curation'}))
 fireEvent.click(screen.getByRole('button',{name:'Cancel import'}));resolve(preview)
 expect(await screen.findByText('Import cancelled. No references were saved.')).toBeVisible()
 expect(api.command).toHaveBeenCalledWith('cancel',{identifier:'draft'})
 expect(screen.queryByRole('button',{name:'Add references'})).toBeNull()
})
it('retries acceptance with the same explicit payload after a connection failure',async()=>{
 const api=client();api.command=vi.fn().mockRejectedValueOnce(new Error('Connection interrupted')).mockResolvedValue({...preview,status:'accepted'})
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} initialDraftId="draft" pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('radio',{name:'Editorial: publish'}));fireEvent.change(screen.getByLabelText('Explanation for Editorial'),{target:{value:'Reason'}})
 fireEvent.click(screen.getByRole('button',{name:'Add references'}));expect(await screen.findByRole('alert')).toHaveTextContent('Connection interrupted')
 fireEvent.click(screen.getByRole('button',{name:'Add references'}));expect(await screen.findByText('References saved.')).toBeVisible()
 expect(vi.mocked(api.command).mock.calls[0]).toEqual(vi.mocked(api.command).mock.calls[1])
})
it('discards and cancels a late preview when the host switches item lists',async()=>{
 let resolve!:(draft:PrimingDraft)=>void
 const api=client();api.command=vi.fn().mockImplementation(action=>action==='transform'?new Promise<PrimingDraft>(done=>resolve=done):Promise.resolve({...preview,status:'cancelled',rows:[]}))
 const props={client:api,classifiers:[{id:'editor',revision:1}],pollInterval:0}
 const view=render(<PrimingPanel {...props} listId="examples"/> )
 fireEvent.click(await screen.findByRole('button',{name:'From reference / URL'}));fireEvent.change(screen.getByLabelText('Source URL'),{target:{value:'https://example.invalid'}});fireEvent.click(screen.getByRole('button',{name:'Start curation'}))
 view.rerender(<PrimingPanel {...props} listId="another-list"/>);resolve(preview)
 await waitFor(()=>expect(api.command).toHaveBeenCalledWith('cancel',{identifier:'draft'}))
 expect(screen.queryByRole('button',{name:'Add references'})).toBeNull()
})
it('shows a committed acceptance when cancellation loses the transaction race',async()=>{
 let resolve!:(draft:PrimingDraft)=>void
 const api=client();api.command=vi.fn().mockImplementation(action=>action==='accept'?new Promise<PrimingDraft>(done=>resolve=done):Promise.reject(new Error('Accepted examples cannot be cancelled')))
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} initialDraftId="draft" pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('radio',{name:'Editorial: publish'}));fireEvent.change(screen.getByLabelText('Explanation for Editorial'),{target:{value:'Explicit reason'}})
 fireEvent.click(screen.getByRole('button',{name:'Add references'}));fireEvent.click(screen.getByRole('button',{name:'Cancel import'}));resolve({...preview,status:'accepted'})
 expect(await screen.findByText('References saved.')).toBeVisible()
 expect(vi.mocked(api.command).mock.calls.filter(([action])=>action==='cancel')).toHaveLength(1)
})

it('hands the accepted draft and classifier pins to the host only after acknowledged acceptance',async()=>{
 const api=client();const accepted={...preview,status:'accepted'};api.command=vi.fn().mockResolvedValue(accepted)
 const onAccepted=vi.fn()
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} initialDraftId="draft" pollInterval={0} onAccepted={onAccepted}/> )
 fireEvent.click(await screen.findByRole('radio',{name:'Editorial: publish'}));fireEvent.change(screen.getByLabelText('Explanation for Editorial'),{target:{value:'Explicit human explanation'}})
 expect(onAccepted).not.toHaveBeenCalled()
 fireEvent.click(screen.getByRole('button',{name:'Add references'}))
 await waitFor(()=>expect(onAccepted).toHaveBeenCalledWith(accepted))
 expect(onAccepted.mock.calls[0][0].contract.classifiers[0].id).toBe('editor')
})

it('accepts a selected label without an explanation and surfaces server rejection without losing it',async()=>{
 const api=client();api.command=vi.fn().mockRejectedValueOnce(new Error('Save unavailable; nothing committed')).mockResolvedValue({...preview,status:'accepted'})
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} initialDraftId="draft" pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('radio',{name:'Editorial: publish'}))
 expect(screen.getByRole('button',{name:'Add references'})).toBeEnabled()
 fireEvent.click(screen.getByRole('button',{name:'Add references'}))
 expect(await screen.findByRole('alert')).toHaveTextContent('Save unavailable')
 expect(screen.getByRole('radio',{name:'Editorial: publish'})).toHaveAttribute('aria-checked','true')
 expect(screen.getByLabelText('Explanation for Editorial')).toHaveValue('')
 fireEvent.click(screen.getByRole('button',{name:'Add references'}))
 expect(await screen.findByText('References saved.')).toBeVisible()
 expect(api.command).toHaveBeenLastCalledWith('accept',expect.objectContaining({labels:[{editor:{label:'publish',rationale:''}}]}))
})
it('downloads the CSV template with a text MIME type',async()=>{
 const api=client();api.template=vi.fn().mockResolvedValue({content:'a,b',encoding:'utf-8'})
 const createObjectURL=vi.fn().mockReturnValue('blob:template')
 vi.stubGlobal('URL',Object.assign(URL,{createObjectURL,revokeObjectURL:vi.fn()}))
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('button',{name:'Download CSV template'}))
 await waitFor(()=>expect(createObjectURL).toHaveBeenCalled())
 expect((createObjectURL.mock.calls[0][0] as Blob).type).toBe('text/csv;charset=utf-8')
 vi.unstubAllGlobals()
})
it('starts an import on a plain-HTTP host where crypto.randomUUID is missing',async()=>{
 const api=client()
 vi.stubGlobal('crypto',{getRandomValues:(bytes:Uint8Array)=>bytes.fill(7)})
 render(<PrimingPanel client={api} listId="examples" classifiers={[{id:'editor',revision:1}]} pollInterval={0}/> )
 fireEvent.click(await screen.findByRole('button',{name:'From reference / URL'}))
 fireEvent.change(screen.getByLabelText('Source URL'),{target:{value:'https://example.invalid'}})
 fireEvent.click(screen.getByRole('button',{name:'Start curation'}))
 await waitFor(()=>expect(api.command).toHaveBeenCalledWith('transform',expect.objectContaining({identifier:expect.stringMatching(/^[0-9a-f-]{36}$/)})))
 vi.unstubAllGlobals()
})
