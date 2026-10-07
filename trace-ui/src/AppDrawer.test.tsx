import {useState} from 'react'
import {cleanup,fireEvent,render,screen,waitFor} from '@testing-library/react'
import {afterEach,expect,test} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {AppDrawer} from './AppDrawer'
afterEach(cleanup)
test('drawer dismissal has an explicit touch target and Escape returns to its opener',async()=>{
  function Example(){const [open,setOpen]=useState(false);return <><button onClick={()=>setOpen(true)}>Open history</button><AppDrawer title="History" open={open} onOpenChange={setOpen} footer={<button>Footer action</button>}>Drawer content</AppDrawer></>}
  render(<Example/> )
  const opener=screen.getByRole('button',{name:'Open history'});opener.focus();fireEvent.click(opener)
  const close=screen.getByRole('button',{name:'Close History'})
  expect(close).toHaveClass('min-h-[44px]','min-w-[44px]')
  expect(screen.getByRole('button',{name:'Footer action'})).toBeVisible()
  expect(screen.getByRole('button',{name:'Footer action'}).parentElement).toHaveClass('[&_button]:min-h-[44px]')
  fireEvent.keyDown(close,{key:'Escape'})
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(()=>expect(opener).toHaveFocus())
})
