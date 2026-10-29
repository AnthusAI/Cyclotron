// Package the existing web console for static Amplify hosting.
// Same-origin BFF rewrites are added only after the reviewed backend exists.
import {createHash} from 'node:crypto'
import {readFileSync, mkdirSync, writeFileSync, cpSync, rmSync} from 'node:fs'
import path from 'node:path'
const vendor=path.resolve(import.meta.dirname,'../../src/decision_flywheel/vendor/trace-ui')
const output=path.resolve(import.meta.dirname,'../console-dist')
rmSync(output,{recursive:true,force:true});mkdirSync(path.join(output,'assets'),{recursive:true})
for(const file of ['web.js','web.css','THIRD_PARTY_NOTICES.txt'])cpSync(path.join(vendor,file),path.join(output,'assets',file))
const version=file=>createHash('sha256').update(readFileSync(path.join(vendor,file))).digest('hex').slice(0,16)
writeFileSync(path.join(output,'index.html'),`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover"><title>Cyclotron Lab</title><link rel="stylesheet" href="/assets/web.css?v=${version('web.css')}"></head><body><div id="root" data-account-mode="hosted" data-event-transport="poll"></div><script src="/assets/web.js?v=${version('web.js')}"></script></body></html>\n`)
