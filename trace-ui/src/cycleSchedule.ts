/**
 * When the brand's cycle mark cycles: once `firstDelayMs` after it first becomes visible, then every `intervalMs`.
 * It only counts time while visible (on screen and the tab showing): going hidden stops the clock, and becoming
 * visible again waits a full interval before the next cycle. Pure timers, so a spec can drive it with fake timers.
 */
export interface CycleSchedule{
  /** Tell the schedule whether the mark is currently visible (in the viewport and the tab showing). */
  setVisible:(visible:boolean)=>void
  dispose:()=>void
}

export const DEFAULT_FIRST_DELAY_MS=3000
export const DEFAULT_INTERVAL_MS=10000

export function createCycleSchedule({firstDelayMs=DEFAULT_FIRST_DELAY_MS,intervalMs=DEFAULT_INTERVAL_MS,onCycle}:{firstDelayMs?:number,intervalMs?:number,onCycle:()=>void}):CycleSchedule{
  let timer:ReturnType<typeof setTimeout>|undefined
  let started=false
  let visible=false
  const fire=()=>{
    onCycle()
    timer=setTimeout(fire,intervalMs)
  }
  return {
    setVisible(next){
      if(next===visible)return
      visible=next
      if(timer!==undefined){clearTimeout(timer);timer=undefined}
      if(!visible)return
      timer=setTimeout(fire,started?intervalMs:firstDelayMs)
      started=true
    },
    dispose(){
      if(timer!==undefined)clearTimeout(timer)
      timer=undefined
      visible=false
    },
  }
}
