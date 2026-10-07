import {useEffect,useRef,useState} from 'react'
import {Button} from '@/components/ui/button'

export function ReplayControls({busy,finished,failed=false,onAdvance}:{busy:boolean;finished:boolean;failed?:boolean;onAdvance:()=>void}){
  const [playing,setPlaying]=useState(false)
  const issued=useRef(false)
  useEffect(()=>{
    if(finished||failed){setPlaying(false);return}
    if(busy){issued.current=false;return}
    if(!playing||finished||failed||issued.current)return
    issued.current=true;onAdvance()
  },[playing,busy,finished,failed,onAdvance])
  const stopped=finished||failed
  return <div className="flex shrink-0 flex-wrap items-center gap-2">
    <Button variant="outline" disabled={busy||finished} onClick={onAdvance}>Step replay</Button>
    <Button disabled={finished||failed} onClick={()=>{issued.current=false;setPlaying(value=>!value)}}>{playing&&!stopped?'Pause replay':'Run replay'}</Button>
    <p className="text-xs text-muted-foreground">{finished?'Replay complete':failed?'Replay stopped; inspect the failure before retrying':busy?'Processing a replay cycle':playing?'Replaying frozen feedback':'Paused'} · Pause takes effect after the current cycle.</p>
  </div>
}
