import {afterEach,expect,test,vi} from 'vitest'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import '@testing-library/jest-dom/vitest'
import {FeedbackControls} from './FeedbackControls'
import type {TraceEvent} from './graphql'

afterEach(cleanup)
const classifiers=[{id:'a',name:'Relevance',config:{classes:[{label:'yes'},{label:'no'}]}}]
const events:TraceEvent[]=[{sequence:1,sourceId:'vote',payload:{kind:'human-feedback',action:'submitted',classifier_id:'a',feedback:{id:'vote:a',item_id:'paper',final_answer_value:'yes',edit_comment_value:'Original reason',review_provenance:'interactive-human-vote'}}}]
const completed={id:'job',kind:'correct',status:'completed',result:{corrected:'paper'}}

test('closing and reopening a correction retains its unsaved explanation and original vote identity',async()=>{
  const submit=vi.fn().mockResolvedValue(completed)
  render(<FeedbackControls classifiers={classifiers} events={events} busy={false} jobs={[]} onSubmit={submit} onResume={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'My unsaved explanation'}})
  fireEvent.click(screen.getByRole('button',{name:'Close Edit recorded feedback'}))
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  expect(screen.getByLabelText('Explanation for Relevance')).toHaveValue('My unsaved explanation')
  expect(submit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Save correction'}))
  await waitFor(()=>expect(submit).toHaveBeenCalledWith('correct',{item_id:'paper',labels:[{classifier_id:'a',label:'yes',comment:'My unsaved explanation',expected_feedback_id:'vote:a'}]}))
})

test('switching reviewed items preserves separate correction drafts without changing their expected identities',async()=>{
  const second={sequence:2,sourceId:'second',payload:{...events[0].payload,feedback:{...(events[0].payload.feedback as object),id:'second:a',item_id:'second',edit_comment_value:'Second reason'}}}
  render(<FeedbackControls classifiers={classifiers} events={[...events,second]} busy={false} jobs={[]} onSubmit={vi.fn()} onResume={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'Second draft'}})
  fireEvent.change(screen.getByLabelText('Reviewed item'),{target:{value:'paper'}})
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'First draft'}})
  fireEvent.change(screen.getByLabelText('Reviewed item'),{target:{value:'second'}})
  expect(screen.getByLabelText('Explanation for Relevance')).toHaveValue('Second draft')
  fireEvent.change(screen.getByLabelText('Reviewed item'),{target:{value:'paper'}})
  expect(screen.getByLabelText('Explanation for Relevance')).toHaveValue('First draft')
})

test('completing one correction does not discard an unsaved draft for another item',async()=>{
  const second={sequence:2,sourceId:'second',payload:{...events[0].payload,feedback:{...(events[0].payload.feedback as object),id:'second:a',item_id:'second',edit_comment_value:'Second reason'}}}
  const submit=vi.fn().mockResolvedValue(completed)
  render(<FeedbackControls classifiers={classifiers} events={[...events,second]} busy={false} jobs={[]} onSubmit={submit} onResume={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'Second unsaved explanation'}})
  fireEvent.change(screen.getByLabelText('Reviewed item'),{target:{value:'paper'}})
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'First corrected explanation'}})
  fireEvent.click(screen.getByRole('button',{name:'Save correction'}))
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull())
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  expect(screen.getByLabelText('Explanation for Relevance')).toHaveValue('Second unsaved explanation')
})

test('correction sends the changed label and explanation with the exact current feedback identity',async()=>{
  const submit=vi.fn().mockResolvedValue(completed)
  render(<FeedbackControls classifiers={classifiers} events={events} busy={false} jobs={[]} onSubmit={submit} onResume={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  fireEvent.change(screen.getByLabelText('Label for Relevance'),{target:{value:'no'}})
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'Actually, no'}})
  fireEvent.click(screen.getByRole('button',{name:'Save correction'}))
  await waitFor(()=>expect(submit).toHaveBeenCalledWith('correct',{item_id:'paper',labels:[{classifier_id:'a',label:'no',comment:'Actually, no',expected_feedback_id:'vote:a'}]}))
})

test('undo requires confirmation and does not submit anything merely by opening its drawer',async()=>{
  const submit=vi.fn().mockResolvedValue({id:'undo',kind:'undo',status:'completed',result:{undone:'paper'}})
  render(<FeedbackControls classifiers={classifiers} events={events} busy={false} jobs={[]} onSubmit={submit} onResume={vi.fn()}/>)
  fireEvent.click(screen.getByRole('button',{name:'Undo item labels'}))
  expect(submit).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button',{name:'Retract labels and review again'}))
  await waitFor(()=>expect(submit).toHaveBeenCalledWith('undo',{}))
})

test('recovery targets the original failed feedback job and never offers paid job retries',async()=>{
  const resume=vi.fn().mockResolvedValue({id:'failed',kind:'undo',status:'pending',result:null})
  const props={classifiers,events,busy:false,onSubmit:vi.fn(),onResume:resume}
  const {rerender}=render(<FeedbackControls {...props} jobs={[{id:'failed',kind:'undo',status:'failed',result:null}]}/>)
  fireEvent.click(screen.getByRole('button',{name:'Recover feedback command'}))
  await waitFor(()=>expect(resume).toHaveBeenCalledWith('failed'))
  rerender(<FeedbackControls {...props} jobs={[{id:'paid',kind:'optimize',status:'failed',result:null}]}/>)
  expect(screen.queryByRole('button',{name:'Recover feedback command'})).toBeNull()
})

test('a concurrent feedback update cannot silently replace the identity of an open edit',async()=>{
  const submit=vi.fn().mockResolvedValue(completed)
  const props={classifiers,busy:false,jobs:[],onSubmit:submit,onResume:vi.fn()}
  const {rerender}=render(<FeedbackControls {...props} events={events}/>)
  fireEvent.click(screen.getByRole('button',{name:'Edit recorded feedback'}))
  fireEvent.change(screen.getByLabelText('Explanation for Relevance'),{target:{value:'My pending edit'}})
  rerender(<FeedbackControls {...props} events={[...events,{sequence:2,sourceId:'other-edit',payload:{...events[0].payload,feedback:{...(events[0].payload.feedback as object),id:'other:a',edit_comment_value:'Someone else changed this'}}}]}/>)
  fireEvent.click(screen.getByRole('button',{name:'Save correction'}))
  await waitFor(()=>expect(submit.mock.calls[0][1].labels[0].expected_feedback_id).toBe('vote:a'))
})

test('inherited source feedback is read only and frozen replay has no editing controls',()=>{
  const inherited=[{...events[0],payload:{...events[0].payload,feedback:{...(events[0].payload.feedback as object),review_provenance:'replayed-human-vote:source'}}}]
  const props={classifiers,busy:false,jobs:[],onSubmit:vi.fn(),onResume:vi.fn()}
  const {rerender}=render(<FeedbackControls {...props} events={inherited}/>)
  expect(screen.getByRole('button',{name:'Edit recorded feedback'})).toBeDisabled()
  expect(screen.getByRole('button',{name:'Undo item labels'})).toBeDisabled()
  rerender(<FeedbackControls {...props} events={events} replay/>)
  expect(screen.queryByRole('button')).toBeNull()
})
