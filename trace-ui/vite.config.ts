import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'
import tailwindcss from '@tailwindcss/vite'
import path from 'node:path'

// https://vite.dev/config/
export default defineConfig({
  define: {'process.env.NODE_ENV': JSON.stringify('production')},
  plugins: [react(),tailwindcss()],
  resolve: {alias: {'@': path.resolve(import.meta.dirname,'src')}},
  build: {
    outDir: '../src/decision_flywheel/vendor/trace-ui',
    emptyOutDir: true,
    copyPublicDir: false,
    assetsInlineLimit: 1000000,
    lib: {entry:'src/main.tsx',name:'TraceViewer',formats:['iife'],fileName:()=> 'viewer.js',cssFileName:'viewer'},
  },
})
