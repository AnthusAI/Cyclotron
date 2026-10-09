import {cleanup,fireEvent,render,screen,waitFor,within} from '@testing-library/react'
import {afterEach,expect,test,vi} from 'vitest'
import '@testing-library/jest-dom/vitest'
import {MobileNavigation} from './MobileNavigation'
afterEach(cleanup)

test('the full-screen menu hides background controls and restores focus after Escape',async()=>{
  const navigate=vi.fn()
  render(<><button>Background action</button><MobileNavigation section="optimizations" onNavigate={navigate}/></>)
  const trigger=screen.getByRole('button',{name:'Open main menu'})
  trigger.focus();fireEvent.click(trigger)
  const menu=screen.getByRole('dialog',{name:'Navigate Cyclotron'})
  expect(screen.queryByRole('button',{name:'Background action'})).toBeNull()
  expect(within(menu).getByRole('button',{name:'Close main menu'})).toBeVisible()
  fireEvent.keyDown(document.activeElement!,{key:'Escape'})
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull())
  await waitFor(()=>expect(trigger).toHaveFocus())
  expect(navigate).not.toHaveBeenCalled()
})

test('selecting a workspace closes the menu and marks the current workspace accessibly',async()=>{
  const navigate=vi.fn()
  render(<MobileNavigation section="cyclotrons" onNavigate={navigate}/>)
  fireEvent.click(screen.getByRole('button',{name:'Open main menu'}))
  const menu=screen.getByRole('dialog',{name:'Navigate Cyclotron'})
  expect(within(menu).getByRole('button',{name:/Cyclotrons/})).toHaveAttribute('aria-current','page')
  fireEvent.click(within(menu).getByRole('button',{name:/Item lists/}))
  expect(navigate).toHaveBeenCalledWith('items')
  await waitFor(()=>expect(screen.queryByRole('dialog')).toBeNull())
})
