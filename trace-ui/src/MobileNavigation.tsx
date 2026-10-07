import {useState} from 'react'
import {Dialog} from 'radix-ui'
import {Menu,X} from 'lucide-react'
import {Button} from '@/components/ui/button'
import {CyclotronBrand} from './CyclotronBrand'
import {navigationItems} from './navigation'
export function MobileNavigation({section,onNavigate}:{section:typeof navigationItems[number]['id'];onNavigate:(section:typeof navigationItems[number]['id'])=>void}){
  const [open,setOpen]=useState(false)
  return <Dialog.Root open={open} onOpenChange={setOpen}>
    <Dialog.Trigger asChild><Button variant="ghost" size="icon" className="sm:hidden min-h-[44px] min-w-[44px]" aria-label="Open main menu"><Menu/></Button></Dialog.Trigger>
    <Dialog.Portal><Dialog.Overlay className="fixed inset-0 z-60 bg-black/35"/>
      <Dialog.Content className="fixed inset-0 z-60 flex flex-col bg-background text-foreground">
        <Dialog.Title className="sr-only">Navigate Cyclotron</Dialog.Title>
        <div className="flex shrink-0 items-center justify-between gap-3 border-b border-border px-4 py-3 pt-[max(0.75rem,env(safe-area-inset-top))]"><CyclotronBrand/><Dialog.Close asChild><Button variant="ghost" size="icon" className="min-h-[44px] min-w-[44px]" aria-label="Close main menu"><X/></Button></Dialog.Close></div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-5"><Dialog.Description className="text-sm leading-relaxed text-muted-foreground">Choose a workspace. Each view keeps its own context and returns here without restarting a session.</Dialog.Description><nav aria-label="Mobile navigation" className="mt-8 grid gap-3">{navigationItems.map(item=><Button key={item.id} aria-current={section===item.id?'page':undefined} variant={section===item.id?'secondary':'outline'} className="h-auto min-h-24 items-start justify-start whitespace-normal px-5 py-4 text-left" onClick={()=>{onNavigate(item.id);setOpen(false)}}><span><span className="block text-base font-semibold">{item.label}</span><span className="mt-1 block text-sm font-normal leading-relaxed text-muted-foreground">{item.description}</span></span></Button>)}</nav></div>
      </Dialog.Content>
    </Dialog.Portal>
  </Dialog.Root>
}
