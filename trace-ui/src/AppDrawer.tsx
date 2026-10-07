import {useRef,type ReactNode} from 'react'
import {Dialog} from 'radix-ui'
import {X} from 'lucide-react'
import {Button} from '@/components/ui/button'

export function AppDrawer({title,side='left',open,onOpenChange,children,footer}:{title:string;side?:'left'|'right';open:boolean;onOpenChange:(open:boolean)=>void;children:ReactNode;footer?:ReactNode}){
  const opener=useRef<HTMLElement|null>(null)
  return <Dialog.Root open={open} onOpenChange={onOpenChange}><Dialog.Portal>
    <Dialog.Overlay className="fixed inset-0 z-50 bg-black/35"/>
    <Dialog.Content onOpenAutoFocus={()=>{opener.current=document.activeElement instanceof HTMLElement?document.activeElement:null}} onCloseAutoFocus={event=>{if(opener.current?.isConnected){event.preventDefault();opener.current.focus()}}} className={`fixed inset-y-0 z-50 flex w-[min(92vw,440px)] flex-col border-border bg-background shadow-xl ${side==='left'?'left-0 border-r':'right-0 border-l'}`}>
      <div className="flex shrink-0 items-center justify-between border-b px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))]"><Dialog.Title className="text-base font-semibold">{title}</Dialog.Title><Dialog.Close asChild><Button variant="ghost" size="icon" className="min-h-[44px] min-w-[44px]" aria-label={`Close ${title}`}><X/></Button></Dialog.Close></div>
      <Dialog.Description className="sr-only">Inspect {title.toLowerCase()} without leaving the current workspace.</Dialog.Description>
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto p-4">{children}</div>
      {footer?<div className="flex shrink-0 flex-wrap justify-end gap-2 border-t p-4 pb-[max(1rem,env(safe-area-inset-bottom))] [&_button]:min-h-[44px] [&_button]:min-w-[44px]">{footer}</div>:null}
    </Dialog.Content>
  </Dialog.Portal></Dialog.Root>
}
