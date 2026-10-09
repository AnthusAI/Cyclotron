import {cleanup,fireEvent,render,screen} from '@testing-library/react'
import {afterEach,it,expect,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {MultiLabelCard} from './MultiLabelCard'
afterEach(()=>{cleanup();sessionStorage.clear()})

it('provides a named keyboard focus target for the scrollable classifier feedback panel',()=>{
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'focus',classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']}}}}
  render(<MultiLabelCard current={current} names={{a:'Library'}} busy={false} onSubmit={vi.fn()}/>)
  const panel=screen.getByRole('group',{name:'Classifier feedback'})
  expect(panel).toHaveAttribute('tabindex','0')
  panel.focus()
  expect(panel).toHaveFocus()
})

it('shows pinned cyclotron order instead of response key order without moving votes between classifiers',()=>{
  const result={label:'yes',confidence:.8,classes:['yes','no']}
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'ordered',classifiers:{a:result,b:result,c:result}}}
  const submit=vi.fn()
  const props={current,names:{a:'First response key',b:'First configured',c:'Unconfigured output'},busy:false,onSubmit:submit}
  const view=render(<MultiLabelCard {...props} classifiers={[{id:'b',name:'First configured'},{id:'a',name:'First response key'}]}/>)
  expect(screen.getAllByRole('heading',{level:3}).map(el=>el.textContent)).toEqual(['First configured','First response key','Unconfigured output'])
  fireEvent.click(screen.getByRole('button',{name:'First configured: no'}))
  fireEvent.change(screen.getByLabelText('Explanation for First configured'),{target:{value:'Feedback belongs to b'}})
  fireEvent.click(screen.getByRole('button',{name:'First response key: yes, predicted 80%'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(submit).toHaveBeenLastCalledWith('label',{item_id:'one',presentation_id:'ordered',labels:[
    {classifier_id:'b',label:'no',comment:'Feedback belongs to b'},{classifier_id:'a',label:'yes',comment:''}]})
  view.rerender(<MultiLabelCard {...props} classifiers={[{id:'a',name:'First response key'},{id:'b',name:'First configured'}]}/>)
  expect(screen.getByRole('button',{name:'First configured: no'})).toHaveAttribute('aria-pressed','true')
  expect(screen.getByLabelText('Explanation for First configured')).toHaveValue('Feedback belongs to b')
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(submit).toHaveBeenLastCalledWith('label',{item_id:'one',presentation_id:'ordered',labels:[
    {classifier_id:'a',label:'yes',comment:''},{classifier_id:'b',label:'no',comment:'Feedback belongs to b'}]})
})

it('restores explicit votes and explanations when returning to an item without remounting the reviewer',()=>{
  const submit=vi.fn()
  const item=(id:string)=>({item:{id,values:{text:`Paper ${id}`}},prediction:{presentation_id:id,classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']}}}})
  const review=render(<MultiLabelCard current={item('first')} names={{a:'Library'}} busy={false} onSubmit={submit}/>)
  fireEvent.click(screen.getByRole('button',{name:'Library: no'}))
  fireEvent.change(screen.getByLabelText('Explanation for Library'),{target:{value:'Not about knowledge bases'}})
  review.rerender(<MultiLabelCard current={item('second')} names={{a:'Library'}} busy={false} onSubmit={submit}/>)
  expect(screen.getByRole('button',{name:'Library: no'})).toHaveAttribute('aria-pressed','false')
  expect(screen.getByLabelText('Explanation for Library')).toHaveValue('')
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
  review.rerender(<MultiLabelCard current={item('first')} names={{a:'Library'}} busy={false} onSubmit={submit}/>)
  expect(screen.getByRole('button',{name:'Library: no'})).toHaveAttribute('aria-pressed','true')
  expect(screen.getByLabelText('Explanation for Library')).toHaveValue('Not about knowledge bases')
  expect(submit).not.toHaveBeenCalled()
})

it('replays recorded labels without asking the human to vote on them again',()=>{
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'shown',classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']},b:{label:'no',confidence:.7,classes:['yes','no']}},recorded_labels:[{classifier_id:'a',label:'no',comment:'Original explanation'}]}}
  const submit=vi.fn()
  render(<MultiLabelCard current={current} names={{a:'Old',b:'New'}} busy={false} onSubmit={submit} />)
  expect(screen.getByRole('button',{name:'Old: no'})).toBeDisabled()
  expect(screen.getByLabelText('Explanation for Old')).toHaveValue('Original explanation')
  fireEvent.click(screen.getByRole('button',{name:'New: yes'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(submit).toHaveBeenCalledWith('label',{item_id:'one',presentation_id:'shown',labels:[{classifier_id:'b',label:'yes',comment:''}]})
})

it('keeps each classifier metrics beneath its voting controls and feedback beside actions',()=>{
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'shown',classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']}}}}
  render(<MultiLabelCard current={current} names={{a:'Library'}} busy={false} onSubmit={vi.fn()} classifiers={[{id:'a',name:'Library'}]} events={[]} footer={<span>Labels saved</span>} />)
  const controls=screen.getByRole('group',{name:'Library classification'})
  const metrics=screen.getByRole('region',{name:'Library metrics'})
  expect(controls.compareDocumentPosition(metrics)&Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  expect(screen.getByRole('group',{name:'Review actions'}).parentElement).toContainElement(screen.getByText('Labels saved'))
})

it('shows binary confidence aligned with the configured class order',()=>{
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'shown',classifiers:{a:{label:'no',confidence:.8,classes:['yes','no']}}}}
  render(<MultiLabelCard current={current} names={{a:'Library'}} busy={false} onSubmit={vi.fn()} />)
  const meter=screen.getByRole('meter',{name:'Library prediction confidence'})
  expect(meter).toHaveAttribute('aria-valuetext','yes: 20%, no: 80%')
  expect(meter.querySelector('[data-confidence-class="yes"]')).toHaveStyle({flexGrow:'0.19999999999999996'})
  expect(meter.querySelector('[data-confidence-class="no"]')).toHaveStyle({flexGrow:'0.8'})
})

it('submits three classifier labels together for one item',()=>{
  const submit=vi.fn()
  const current={item:{id:'paper',values:{text:'Paper'}},prediction:{presentation_id:'shown',classifiers:Object.fromEntries(['a','b','c'].map(id=>[id,{label:'include',confidence:.7,classes:['include','exclude']}]))}}
  render(<MultiLabelCard current={current} names={{a:'Knowledge Bases',b:'Threat Intelligence',c:'Anthus'}} busy={false} onSubmit={submit} />)
  fireEvent.click(screen.getByRole('button',{name:'Knowledge Bases: include, predicted 70%'}))
  fireEvent.click(screen.getByRole('button',{name:'Threat Intelligence: exclude'}))
  fireEvent.click(screen.getByRole('button',{name:'Anthus: include, predicted 70%'}))
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(submit).toHaveBeenCalledWith('label',{item_id:'paper',presentation_id:'shown',labels:[{classifier_id:'a',label:'include',comment:''},{classifier_id:'b',label:'exclude',comment:''},{classifier_id:'c',label:'include',comment:''}]})
  const actions=screen.getByRole('group',{name:'Review actions'})
  expect(Array.from(actions.querySelectorAll('button')).map(button=>button.textContent)).toEqual(['Skip item','Submit review'])
})

it('shows independent predictions and submits only explicit classifier labels with explanations',()=>{
  const submit=vi.fn()
  const current={item:{id:'one',values:{text:'Paper content'}},prediction:{presentation_id:'shown',classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']},b:{label:'science',confidence:.6,classes:['science','sport','business']}}}}
  render(<MultiLabelCard current={current} names={{a:'Library',b:'Topic'}} busy={false} onSubmit={submit} />)
  expect(screen.getByRole('button',{name:'Library: yes, predicted 80%'})).toBeVisible()
  fireEvent.click(screen.getByRole('button',{name:'Library: no'}))
  fireEvent.change(screen.getByLabelText('Explanation for Library'),{target:{value:'Not relevant'}})
  fireEvent.click(screen.getByRole('button',{name:'Submit review'}))
  expect(submit).toHaveBeenCalledWith('label',{item_id:'one',presentation_id:'shown',labels:[{classifier_id:'a',label:'no',comment:'Not relevant'}]})
})

it('colors agreement green and disagreement red without preselecting a label',()=>{
  const current={item:{id:'one',values:{text:'Paper'}},prediction:{presentation_id:'shown',classifiers:{a:{label:'yes',confidence:.8,classes:['yes','no']}}}}
  render(<MultiLabelCard current={current} names={{a:'Library'}} busy={false} onSubmit={vi.fn()} />)
  const yes=screen.getByRole('button',{name:'Library: yes, predicted 80%'})
  const no=screen.getByRole('button',{name:'Library: no'})
  expect(yes).toHaveAttribute('aria-pressed','false')
  expect(screen.getByRole('button',{name:'Submit review'})).toBeDisabled()
  fireEvent.click(yes)
  expect(yes).toHaveAttribute('data-agreement','correct')
  fireEvent.click(no)
  expect(no).toHaveAttribute('data-agreement','incorrect')
  expect(yes).toHaveAttribute('aria-pressed','false')
  fireEvent.click(no)
  expect(no).toHaveAttribute('aria-pressed','false')
})
