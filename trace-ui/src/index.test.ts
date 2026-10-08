import {readFileSync} from 'node:fs'
import {expect, test} from 'vitest'

test('expanded classifier details cannot consume all reading space or push actions off screen',()=>{
  const css=readFileSync('src/index.css','utf8')
  const rule=css.match(/\[data-labeling-view="true"\] \.classifier-feedback-strip\s*\{([^}]+)\}/)?.[1]??''
  expect(rule).toMatch(/max-height:\s*50%/)
  expect(rule).toMatch(/flex-shrink:\s*1/)
  expect(rule).toMatch(/min-height:\s*0/)
  expect(rule).toMatch(/overflow-y:\s*auto/)
})

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

test('the Cyclotron mark uses the subtitle width instead of a fixed empty grid column',()=>{
  const css=readFileSync('src/styles/shared.css','utf8')
  const layout=css.match(/\.cyclotron-brand-layout\s*\{([^}]+)\}/)
  expect(layout?.[1]).toContain('width:fit-content')
  expect(layout?.[1]).not.toContain('width:17rem')
})

test('the application chrome uses flat surface tiers instead of borders, outlines, shadows, or gradients',()=>{
  const css=readFileSync('src/styles/shared.css','utf8')
  expect(readFileSync('src/index.css','utf8')).toContain('@import "./styles/shared.css"')
  expect(css).toContain('/* Flat, polarity-consistent interface */')
  expect(css).toMatch(/body \*,\nbody \*::before,\nbody \*::after \{[^}]*border:\s*0 !important[^}]*outline:\s*0 !important[^}]*box-shadow:\s*none !important[^}]*background-image:\s*none !important/s)
  expect(css).toMatch(/body :focus-visible \{[^}]*background-color:\s*var\(--accent\) !important[^}]*color:\s*var\(--accent-foreground\) !important/s)
})
