import {expect,test} from 'vitest'
import {metricRates} from './metricRates'

test('multiple positive classes are one positive versus rest group',()=>{
  const rates=metricRates({confusion_matrix:{a:{a:0,b:1,c:0},b:{a:0,b:1,c:0},c:{a:1,b:0,c:0}},per_class:{}},[{label:'a',role:'positive'},{label:'b',role:'positive'},{label:'c',role:'negative'}])
  expect(rates.recall).toBe(1)
  expect(rates.precision).toBeCloseTo(2/3)
  expect(rates.positiveLabels).toEqual(['a','b'])
})
test('macro includes every declared class and does not drop missing support',()=>{
  const rates=metricRates({per_class:{a:{recall:1,precision:.5},b:{recall:0,precision:null}}},[{label:'a'},{label:'b'},{label:'c'}])
  expect(rates.recall).toBeNull()
  expect(rates.precision).toBeNull()
})
test('invalid matrices cannot invent positive group rates',()=>{
  const config=[{label:'a',role:'positive'},{label:'b',role:'positive'},{label:'c'}]
  expect(metricRates({per_class:{},confusion_matrix:{a:{a:1,b:0,c:0}}},config).recall).toBeNull()
})
