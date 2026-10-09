import {execFileSync} from 'node:child_process'
import {readFileSync} from 'node:fs'
import path from 'node:path'
import {expect,test} from 'vitest'

const root=path.resolve(import.meta.dirname,'../..')
const manifest=JSON.parse(readFileSync(path.join(root,'package.json'),'utf8'))
const packed=(JSON.parse(execFileSync('npm',['pack','--dry-run','--json'],{cwd:root,encoding:'utf8',stdio:['ignore','pipe','ignore']}))[0].files as {path:string}[]).map(file=>file.path)
const sources=packed.filter(file=>/\.(tsx?|css)$/.test(file))

test('npm pack ships every export target',()=>{
  for(const [name,target] of Object.entries(manifest.exports as Record<string,string>)){
    if(name.includes('*'))continue
    expect(packed,`${name} -> ${target}`).toContain(target.replace(/^\.\//,''))
  }
})

test('packed sources resolve within the tarball and never use the @/ alias',()=>{
  for(const file of sources){
    const text=readFileSync(path.join(root,file),'utf8')
    const specifiers=[...text.matchAll(/(?:from|import)\s*['"]([^'"]+)['"]/g)].map(match=>match[1])
    for(const specifier of specifiers){
      expect(specifier,`${file} imports ${specifier}`).not.toMatch(/^@\//)
      if(!specifier.startsWith('.'))continue
      const base=path.posix.join(path.posix.dirname(file),specifier)
      const candidates=[base,`${base}.ts`,`${base}.tsx`,`${base}/index.ts`]
      expect(candidates.some(candidate=>packed.includes(candidate)),`${file} imports ${specifier}`).toBe(true)
    }
  }
})

test('the embeddable surfaces are exported',()=>{
  for(const name of ['./components/review-control','./components/cyclotron-status','./cyclotron-status','./sdk-types','./styles/components.css'])
    expect(manifest.exports).toHaveProperty([name])
})
