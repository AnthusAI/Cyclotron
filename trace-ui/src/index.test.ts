import {readFileSync} from 'node:fs'
import {expect, test} from 'vitest'

test('timeline items cannot scroll independently of their lane labels',()=>{
  const css=readFileSync('src/index.css','utf8')
  expect(css).toMatch(/\.vis-panel\.vis-center\s*\{[^}]*overflow:clip!important/)
})

test('cycle background bands use shading without vertical boundary strokes',()=>{
  const css=readFileSync('src/index.css','utf8')
  for(const band of ['even','odd','history']) {
    const rule=css.match(new RegExp(`\\.vis-item\\.vis-background\\.cycle-band-${band} \\{([^}]+)\\}`))
    expect(rule?.[1]).toContain('background:')
    expect(rule?.[1]).toContain('border:0!important')
    expect(rule?.[1]).not.toMatch(/border-(left|right):/)
  }
})
