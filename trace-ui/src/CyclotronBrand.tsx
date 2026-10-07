import {RefreshCwWide} from '@/components/ui/refresh-cw-wide'

export function CyclotronBrand(){
  return <div data-testid="cyclotron-brand-layout" className="cyclotron-brand-layout shrink-0 text-foreground">
    <p className="cyclotron-brand">Cyclotron</p>
    <span data-testid="cyclotron-cycle-mark" className="cyclotron-cycle-mark"><RefreshCwWide size={20} aria-hidden="true" /></span>
    <p className="cyclotron-tagline">SELF-ALIGNING DECISION MODEL HARNESS</p>
  </div>
}
