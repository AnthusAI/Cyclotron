import {useRef,type KeyboardEvent,type ReactNode} from 'react'

export type RadioChoice={
  value:string
  content:ReactNode
  ariaLabel?:string
  className?:string
  /** Extra attributes for the button, for example data-* hooks. */
  attrs?:Record<string,string|undefined>
}

/**
 * Visible choices in a radio-group pattern: role=radiogroup with role=radio
 * buttons, aria-checked, one tab stop and arrow-key selection. A rating or
 * label choice is always visible buttons, never a dropdown.
 * `clearable` lets the checked choice be chosen again to clear it.
 */
export function RadioChoices({labelledBy,ariaLabel,choices,value,onChange,disabled=false,clearable=false,className,itemClassName}:{
  labelledBy?:string;ariaLabel?:string;choices:RadioChoice[];value:string;onChange:(value:string)=>void;disabled?:boolean;clearable?:boolean;className?:string;itemClassName?:string
}){
  const refs=useRef<(HTMLButtonElement|null)[]>([])
  const checkedIndex=choices.findIndex(choice=>choice.value===value)
  const tabbable=checkedIndex>=0?checkedIndex:0
  const move=(event:KeyboardEvent<HTMLButtonElement>,index:number)=>{
    const step=event.key==='ArrowRight'||event.key==='ArrowDown'?1:event.key==='ArrowLeft'||event.key==='ArrowUp'?-1:0
    if(!step)return
    event.preventDefault()
    const next=(index+step+choices.length)%choices.length
    onChange(choices[next].value)
    refs.current[next]?.focus()
  }
  return <div role="radiogroup" aria-labelledby={labelledBy} aria-label={labelledBy?undefined:ariaLabel} className={className}>
    {choices.map((choice,index)=>{
      const checked=index===checkedIndex
      return <button key={choice.value} ref={node=>{refs.current[index]=node}} type="button" role="radio" aria-checked={checked} aria-label={choice.ariaLabel}
        disabled={disabled} tabIndex={index===tabbable?0:-1} className={[itemClassName,choice.className].filter(Boolean).join(' ')||undefined} {...choice.attrs}
        onKeyDown={event=>move(event,index)} onClick={()=>onChange(checked&&clearable?'':choice.value)}>{choice.content}</button>
    })}
  </div>
}
