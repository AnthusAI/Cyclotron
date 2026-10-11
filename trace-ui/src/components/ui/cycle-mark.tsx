import * as React from 'react'
import {RefreshCwWide} from './refresh-cw-wide'

/**
 * The Cyclotron cycle mark: `RefreshCwWide`, plus one animation. At rest it IS `RefreshCwWide`.
 *
 * One cycle moves both arrows half the way round the racetrack and lands on the logo pose again, because the
 * mark looks the same after half a turn. The motion happens ALONG the racetrack (a dash offset and arrowheads
 * that ride the same path), never as a rotate transform: the mark is wide (1.75:1) and matches itself at 180
 * degrees, not 90, so rotating it would sweep through tall poses.
 *
 * While a cycle runs the mark is drawn from one closed path, `CYCLE_LOOP`, whose geometry is derived from the
 * logo's: arrow 1, a short drop, arrow 2 (arrow 1 turned half a turn about the center), and a short rise. Its
 * pose at the start and at the end of a cycle matches the logo exactly. No dependencies; requestAnimationFrame
 * only. Under `prefers-reduced-motion: reduce` a cycle does nothing and the mark stays still.
 */
export const CYCLE_LOOP='M3 12a9 9 0 0 1 9-9h18a9.75 9.75 0 0 1 6.74 2.74L39 8V12a9 9 0 0 1-9 9H12a9.75 9.75 0 0 1-6.74-2.74L3 16Z'

/** Geometry of arrow 1 in the 42 x 24 viewBox: a quarter circle, a straight run, a wide arc, a diagonal, and the drop to arrow 2. */
const R1=9
const R3=9.75
const P0={x:30,y:3}
const P1={x:36.74,y:5.74}
const TIP={x:39,y:8}
const DROP=4
const ARM=5

/** Center of the wide arc from P0 to P1 (radius R3) that runs clockwise on screen, and its sweep in radians. */
function wideArc(){
  const mx=(P0.x+P1.x)/2
  const my=(P0.y+P1.y)/2
  const dx=P1.x-P0.x
  const dy=P1.y-P0.y
  const d=Math.hypot(dx,dy)
  const h=Math.sqrt(R3*R3-(d/2)*(d/2))
  for(const sign of [1,-1]){
    const cx=mx+sign*h*(-dy/d)
    const cy=my+sign*h*(dx/d)
    const a0=Math.atan2(P0.y-cy,P0.x-cx)
    let a1=Math.atan2(P1.y-cy,P1.x-cx)
    while(a1<a0)a1+=2*Math.PI
    if(a1-a0<Math.PI)return {cx,cy,a0,sweep:a1-a0}
  }
  throw new Error('no arc')
}
const ARC3=wideArc()
const L1=R1*Math.PI/2
const L2=18
const L3=R3*ARC3.sweep
const L4=Math.hypot(TIP.x-P1.x,TIP.y-P1.y)
/** Length of arrow 1 along the loop, to its tip. */
export const CYCLE_ARC=L1+L2+L3+L4
/** Half the loop: arrow 1, then the drop. Advancing by this much swaps the two arrows, which is the logo pose again. */
export const CYCLE_HALF=CYCLE_ARC+DROP
export const CYCLE_PERIMETER=2*CYCLE_HALF

export interface Tip{x:number,y:number,angle:number}

/** Position and direction of travel after `length` along the loop (wraps). */
export function tipAt(length:number):Tip{
  const total=CYCLE_PERIMETER
  let l=((length%total)+total)%total
  let flip=false
  if(l>=CYCLE_HALF){l-=CYCLE_HALF;flip=true}
  let x:number,y:number,angle:number
  if(l<L1){
    const a=Math.PI+l/R1
    x=12+R1*Math.cos(a);y=12+R1*Math.sin(a);angle=a+Math.PI/2
  }else if(l<L1+L2){
    x=12+(l-L1);y=3;angle=0
  }else if(l<L1+L2+L3){
    const a=ARC3.a0+(l-L1-L2)/R3
    x=ARC3.cx+R3*Math.cos(a);y=ARC3.cy+R3*Math.sin(a);angle=a+Math.PI/2
  }else if(l<CYCLE_ARC){
    const f=(l-L1-L2-L3)/L4
    x=P1.x+(TIP.x-P1.x)*f;y=P1.y+(TIP.y-P1.y)*f;angle=Math.atan2(TIP.y-P1.y,TIP.x-P1.x)
  }else{
    x=TIP.x;y=TIP.y+(l-CYCLE_ARC);angle=Math.PI/2
  }
  return flip?{x:42-x,y:24-y,angle:angle+Math.PI}:{x,y,angle}
}

/** The arrowhead at a tip: two arms of length ARM, 45 degrees either side of the way back along the path. */
export function arrowHead(tip:Tip):string{
  const arm=(side:number)=>{
    const back=tip.angle+Math.PI+side*Math.PI/4
    return `${(tip.x+ARM*Math.cos(back)).toFixed(3)} ${(tip.y+ARM*Math.sin(back)).toFixed(3)}`
  }
  return `M${arm(1)} L${tip.x.toFixed(3)} ${tip.y.toFixed(3)} L${arm(-1)}`
}

const ease=(t:number)=>t<0.5?2*t*t:1-Math.pow(-2*t+2,2)/2

export function prefersReducedMotion(){
  return typeof window!=='undefined'&&typeof window.matchMedia==='function'&&window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

export interface CycleMarkHandle{
  /** Play one cycle (half a lap along the racetrack). Does nothing under reduced motion or while one is running. */
  cycle:()=>void
}

export interface CycleMarkProps extends React.SVGProps<SVGSVGElement>{
  /** Height in pixels (width is 1.75 times that), as for RefreshCwWide. */
  size?:number|string
  strokeWidth?:number|string
  color?:string
  /** Play one cycle each time this value changes (not on first render). */
  cycleKey?:number|string
  /** One cycle's length in milliseconds. */
  durationMs?:number
  onCycleEnd?:()=>void
}

export const CycleMark=React.forwardRef<CycleMarkHandle,CycleMarkProps>(function CycleMark({size,width,height,strokeWidth=2,color='currentColor',className,cycleKey,durationMs=1300,onCycleEnd,...props},ref){
  const [t,setT]=React.useState<number|null>(null)
  const frame=React.useRef<number|null>(null)
  const running=React.useRef(false)
  const end=React.useRef(onCycleEnd)
  end.current=onCycleEnd

  const cycle=React.useCallback(()=>{
    if(running.current||prefersReducedMotion())return
    running.current=true
    const start=performance.now()
    const step=(now:number)=>{
      const f=Math.min(1,(now-start)/durationMs)
      if(f>=1){
        running.current=false
        frame.current=null
        setT(null)
        end.current?.()
        return
      }
      setT(ease(f))
      frame.current=requestAnimationFrame(step)
    }
    setT(0)
    frame.current=requestAnimationFrame(step)
  },[durationMs])

  React.useImperativeHandle(ref,()=>({cycle}),[cycle])
  const first=React.useRef(true)
  React.useEffect(()=>{
    if(first.current){first.current=false;return}
    cycle()
  },[cycleKey,cycle])
  React.useEffect(()=>()=>{if(frame.current!==null)cancelAnimationFrame(frame.current)},[])

  if(t===null)return <RefreshCwWide size={size} width={width} height={height} strokeWidth={strokeWidth} color={color} className={className} {...props} />

  const h=height??(size??24)
  const w=width??(typeof h==='number'?h*1.75:`calc(${h} * 1.75)`)
  const arrowA=tipAt(CYCLE_ARC+t*CYCLE_HALF)
  const arrowB=tipAt(CYCLE_ARC+CYCLE_HALF+t*CYCLE_HALF)
  return <svg xmlns="http://www.w3.org/2000/svg" width={w} height={h} viewBox="0 0 42 24" overflow="visible" fill="none" stroke={color} strokeWidth={strokeWidth} strokeLinecap="round" strokeLinejoin="round" className={className} data-cycling="true" {...props}>
    {[0,CYCLE_HALF].map((start)=><path key={start} d={CYCLE_LOOP} strokeDasharray={`${CYCLE_ARC} ${CYCLE_PERIMETER-CYCLE_ARC}`} strokeDashoffset={-(start+t*CYCLE_HALF)} />)}
    <path d={arrowHead(arrowA)} />
    <path d={arrowHead(arrowB)} />
  </svg>
})
CycleMark.displayName='CycleMark'

export default CycleMark
