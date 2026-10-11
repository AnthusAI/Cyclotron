import * as React from 'react'
import {CycleMark,prefersReducedMotion,type CycleMarkHandle} from './components/ui/cycle-mark'
import {createCycleSchedule,DEFAULT_FIRST_DELAY_MS,DEFAULT_INTERVAL_MS} from './cycleSchedule'

/** The Cyclotron logo: wordmark, wide cycle mark, and tagline. Shared with the marketing site. */

export interface CyclotronBrandProps{
  /**
   * Cycle the mark now and then. Off by default, so existing pages are unchanged. `true` uses the defaults: the
   * first cycle 3000 ms after the logo first becomes visible, then one every 10000 ms. The clock runs only while
   * the logo is on screen and the tab is showing, and never under `prefers-reduced-motion: reduce`.
   */
  animate?:boolean|{firstDelayMs?:number,intervalMs?:number}
}

export function CyclotronBrand({animate=false}:CyclotronBrandProps={}){
  const layout=React.useRef<HTMLDivElement>(null)
  const mark=React.useRef<CycleMarkHandle>(null)
  const options=animate===false?null:animate===true?{}:animate
  const firstDelayMs=options?.firstDelayMs??DEFAULT_FIRST_DELAY_MS
  const intervalMs=options?.intervalMs??DEFAULT_INTERVAL_MS
  const on=options!==null

  React.useEffect(()=>{
    if(!on||prefersReducedMotion())return
    const schedule=createCycleSchedule({firstDelayMs,intervalMs,onCycle:()=>mark.current?.cycle()})
    let inView=false
    const update=()=>schedule.setVisible(inView&&!document.hidden)
    const observer=typeof IntersectionObserver==='undefined'?null:new IntersectionObserver((entries)=>{
      inView=entries.some((e)=>e.isIntersecting)
      update()
    })
    if(observer&&layout.current)observer.observe(layout.current)
    else inView=true
    document.addEventListener('visibilitychange',update)
    update()
    return ()=>{
      observer?.disconnect()
      document.removeEventListener('visibilitychange',update)
      schedule.dispose()
    }
  },[on,firstDelayMs,intervalMs])

  return <div ref={layout} data-testid="cyclotron-brand-layout" className="cyclotron-brand-layout shrink-0 text-foreground">
    <p className="cyclotron-brand">Cyclotron</p>
    <span data-testid="cyclotron-cycle-mark" className="cyclotron-cycle-mark"><CycleMark ref={mark} size={20} aria-hidden="true" /></span>
    <p className="cyclotron-tagline">SELF-ALIGNING AI DECISIONS</p>
  </div>
}
