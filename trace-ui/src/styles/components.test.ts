import {readFileSync} from 'node:fs'
import {expect,test} from 'vitest'

const components=readFileSync('src/styles/components.css','utf8')
const rules=components.replace(/\/\*[\s\S]*?\*\//g,'')

test('components.css holds component rules only: no global reset and no :root theme',()=>{
  expect(rules).not.toMatch(/:root/)
  expect(rules).not.toMatch(/(^|[\s,}])(html|body)\b/)
  expect(rules).not.toMatch(/(^|[\s,{}])\*(\s|,|::|\{)/)
  expect(rules).not.toMatch(/@import/)
  for(const selector of rules.match(/^[^{}@\n][^{}]*(?=\{)/gm)??[])
    expect(selector.trim()).toMatch(/(\.cyclotron-|\.label-|\.prediction-|\.confidence-key|\[data-confidence-index)/)
})

test('every theme token in components.css has a fallback for a host page',()=>{
  expect(rules.match(/var\(--[a-z-]+\)/g)??[]).toEqual([])
})

test('shared.css imports components.css so there is one source of component rules',()=>{
  const shared=readFileSync('src/styles/shared.css','utf8')
  expect(shared).toContain('@import "./components.css";')
  expect(shared).not.toContain('.cyclotron-review {')
})
