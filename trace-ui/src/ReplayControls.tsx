import {useEffect,useRef,useState} from 'react'
import {Button} from '@/components/ui/button'

export function ReplayControls({busy,finished,failed=false,readOnly=false,onAdvance}:{busy:boolean;finished:boolean;failed?:boolean;readOnly?:boolean;onAdvance:()=>void}){
  const [playing,setPlaying]=useState(false)
  const issued=useRef(false)
  useEffect(()=>{
    if(finished||failed||readOnly){setPlaying(false);return}
    if(busy){issued.current=false;return}
    if(!playing||finished||failed||issued.current)return
    issued.current=true;onAdvance()
  },[playing,busy,finished,failed,readOnly,onAdvance])
  const stopped=finished||failed||readOnly
  if(finished)return <span className="text-xs text-muted-foreground">Replay complete</span>
  return <div className="flex shrink-0 flex-wrap items-center gap-2">
    <Button variant="outline" disabled={busy||finished||readOnly} onClick={onAdvance}>Step replay</Button>
    <Button disabled={finished||failed||readOnly} onClick={()=>{issued.current=false;setPlaying(value=>!value)}}>{playing&&!stopped?'Pause replay':'Run replay'}</Button>
    <p className="text-xs text-muted-foreground">{finished?'Replay complete':failed?'Replay stopped; inspect the failure before retrying':busy?'Processing a replay cycle':playing?'Replaying frozen feedback':'Paused'} · Pause takes effect after the current cycle.</p>
  </div>
}
