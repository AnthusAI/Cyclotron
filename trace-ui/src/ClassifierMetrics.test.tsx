import {render,screen,within} from '@testing-library/react'
import {expect,it} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {ClassifierMetrics} from './ClassifierMetrics'

it('compact metrics use named colored bars in recall precision accuracy order',()=>{
  render(<ClassifierMetrics compact classifiers={[{id:'compact',name:'Compact'}]} events={[]} />)
  expect(within(screen.getByRole('region',{name:'Compact metrics'})).getAllByRole('meter').map(node=>node.getAttribute('aria-label'))).toEqual(['Compact Recall','Compact Precision','Compact Accuracy'])
})

it('shows independent live recall precision and accuracy in that order',()=>{
  const classifiers=[{id:'a',name:'Knowledge Bases',config:{classes:[{label:'include',role:'positive'}]}},{id:'b',name:'Threat Intelligence',config:{classes:[{label:'include',role:'positive'}]}}]
  const events=[{sequence:1,sourceId:'a',payload:{kind:'cycle-metrics',classifier_id:'a',metrics:{count:5,accuracy:.6,per_class:{include:{recall:.5,precision:.75}}}}}]
  const {rerender}=render(<ClassifierMetrics classifiers={classifiers} events={events} />)
  const first=screen.getByRole('region',{name:'Knowledge Bases metrics'})
  expect(within(first).getAllByRole('term').map(node=>node.textContent)).toEqual(['Recall','Precision','Accuracy'])
  expect(within(first).getAllByRole('definition').map(node=>node.textContent)).toEqual(['50.0%','75.0%','60.0%'])
  expect(within(screen.getByRole('region',{name:'Threat Intelligence metrics'})).getAllByRole('definition').map(node=>node.textContent)).toEqual(['—','—','—'])
  rerender(<ClassifierMetrics classifiers={classifiers} events={[...events,{sequence:2,sourceId:'b',payload:{kind:'cycle-metrics',classifier_id:'b',metrics:{count:1,accuracy:1,per_class:{include:{recall:1,precision:1}}}}}]} />)
  expect(within(first).getByText('60.0%')).toBeVisible()
  expect(within(screen.getByRole('region',{name:'Threat Intelligence metrics'})).getAllByRole('definition').map(node=>node.textContent)).toEqual(['100.0%','100.0%','100.0%'])
})
