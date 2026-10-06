import {Checkbox} from '@/components/ui/checkbox'
import {Label} from '@/components/ui/label'

/** Adapt the accessible Radix checkbox to the offline controller's filter contract. */
export function FilterCheckbox({id,label}:{id:string;label:string}) {
  return <div className="flex items-center gap-2"><Checkbox id={id} onCheckedChange={checked=>{
    const element=document.getElementById(id) as HTMLElement & {checked:boolean}
    element.checked=checked===true
    element.dispatchEvent(new Event('change',{bubbles:true}))
  }} /><Label htmlFor={id} className="text-xs font-normal">{label}</Label></div>
}
