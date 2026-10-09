import {RefreshCwWide} from './components/ui/refresh-cw-wide'

/** The Cyclotron logo: wordmark, wide cycle mark, and tagline. Shared with the marketing site. */

export function CyclotronBrand(){
  return <div data-testid="cyclotron-brand-layout" className="cyclotron-brand-layout shrink-0 text-foreground">
    <p className="cyclotron-brand">Cyclotron</p>
    <span data-testid="cyclotron-cycle-mark" className="cyclotron-cycle-mark"><RefreshCwWide size={20} aria-hidden="true" /></span>
    <p className="cyclotron-tagline">SELF-ALIGNING AI DECISIONS</p>
  </div>
}
