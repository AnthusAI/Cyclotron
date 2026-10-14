import {createRoot} from 'react-dom/client'
import './index.css'
import {WebApp} from './WebApp'
import {AccountConsole} from './AccountConsole'

const root=document.getElementById('root')!
createRoot(root).render(root.dataset.accountMode==='hosted'?<AccountConsole/>:<WebApp />)
