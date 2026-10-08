import {expect,test} from 'vitest'
import {recordedOutcomeWindows} from './recordedOutcomes'

const snapshot=(id:string,cycle:number,count:number)=>({kind:'cycle-metrics',classifier_id:id,cycle_number:cycle,class_config:[{label:'yes',role:'positive'},{label:'no',role:'negative'}],metrics:{count,accuracy:.5,per_class:{yes:{recall:1,precision:.5},no:{recall:0,precision:null}},calibration:{count,ece:.2,brier:.3,bins:[]}}})
test('recorded outcomes keep classifier windows separate and preserve sample sizes',()=>{
  const windows=recordedOutcomeWindows([snapshot('a',1,1),snapshot('b',1,1),snapshot('a',3,3)])
  expect(windows).toHaveLength(2)
  expect(windows[0].first.count).toBe(1)
  expect(windows[0].latest.count).toBe(3)
  expect(windows[0].latest.recall).toBe(1)
  expect(windows[1].latest.count).toBe(1)
})
test('malformed paired output metrics are not rendered as a raw versus final comparison',()=>{
  const event=snapshot('a',1,1)
  expect(recordedOutcomeWindows([{...event,metrics:{...event.metrics,decision_model_comparison:{count:1}}}])[0].latest.rawFinal).toBeUndefined()
})
test('counts and optimizer proposals cannot substitute for measured outcomes',()=>{
  expect(recordedOutcomeWindows([{kind:'prediction',label:'yes'},{kind:'cycle-metrics',metrics:null},{kind:'optimizer-response'}])).toEqual([])
})
