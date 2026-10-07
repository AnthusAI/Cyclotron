import {expect, test} from 'vitest'
import {execFileSync} from 'node:child_process'
import path from 'node:path'
import {JSDOM, VirtualConsole} from 'jsdom'

test('the exported browser bundle mounts without Node globals or runtime startup errors', async () => {
  const sourceRoot=path.resolve(process.cwd(),'..','src')
  const html = execFileSync(process.env.PYTHON ?? 'python3', ['-c',
    'from decision_flywheel.trace_artifact import render_trace; print(render_trace([]))'],
    {cwd: process.cwd(), encoding: 'utf8', maxBuffer: 8 * 1024 * 1024,
      env:{...process.env,PYTHONPATH:[sourceRoot,process.env.PYTHONPATH].filter(Boolean).join(path.delimiter)}})
  const errors: string[] = []
  const console = new VirtualConsole()
  console.on('jsdomError', error => errors.push(error.message))
  const dom = new JSDOM(html, {runScripts: 'dangerously', pretendToBeVisual: true, virtualConsole: console})
  try {
    await new Promise(resolve => setTimeout(resolve, 50))
    expect(errors).toEqual([])
    expect(dom.window.document.getElementById('zoom-in')).not.toBeNull()
    expect(dom.window.document.querySelector('#root [data-slot="card"]')).not.toBeNull()
    expect(dom.window.document.querySelectorAll('#vendor-license')).toHaveLength(1)
  } finally {
    dom.window.close()
  }
})
