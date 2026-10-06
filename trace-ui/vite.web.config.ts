import {defineConfig} from 'vite'
import base from './vite.config.ts'

export default defineConfig({
  ...base,
  build:{...base.build,emptyOutDir:false,
    lib:{entry:'src/web-main.tsx',name:'FlywheelWorkspace',formats:['iife'],fileName:()=> 'web.js',cssFileName:'web'}},
})
