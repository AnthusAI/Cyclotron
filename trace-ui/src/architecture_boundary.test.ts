import {readdirSync,readFileSync} from 'node:fs'
import path from 'node:path'
import {expect,test} from 'vitest'


function sourceFiles(directory:string,includeTests=false):string[]{
  return readdirSync(directory,{withFileTypes:true}).flatMap(entry=>{
    const full=path.join(directory,entry.name)
    if(entry.isDirectory())return sourceFiles(full,includeTests)
    return /\.(ts|tsx)$/.test(entry.name)&&(includeTests||(!entry.name.endsWith('.test.ts')&&!entry.name.endsWith('.test.tsx')))?[full]:[]
  })
}


test('Scenario: The browser does not own optimization logic',()=>{
  const source=sourceFiles(path.join(import.meta.dirname)).map(file=>readFileSync(file,'utf8')).join('\n')
  expect(source).not.toMatch(/(?:OPENAI|TYPESAFE|ANTHROPIC)_(?:API_)?KEY/)
  expect(source).not.toMatch(/fit_learned_head|DecisionFlywheel|OptimizerAgent/)
  expect(source).toContain("fetch('/graphql'")
})


test('each frontend Gherkin scenario has an executable Vitest scenario',()=>{
  const feature=readFileSync(path.join(import.meta.dirname,'..','features','labeling_workspace.feature'),'utf8')
  const tests=sourceFiles(path.join(import.meta.dirname),true).map(file=>readFileSync(file,'utf8')).join('\n')
  const scenarios=[...feature.matchAll(/^\s*Scenario: (.+)$/gm)].map(match=>match[1])
  expect(scenarios).not.toHaveLength(0)
  for(const scenario of scenarios)expect(tests).toContain(`Scenario: ${scenario}`)
})
