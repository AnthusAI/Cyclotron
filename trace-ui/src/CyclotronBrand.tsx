import {RefreshCwWide} from '@/components/ui/refresh-cw-wide'

export function CyclotronBrand(){
  return <div className="shrink-0 text-foreground">
    <div className="flex items-center gap-2">
      <p className="cyclotron-brand">Cyclotron</p>
      <RefreshCwWide size={20} className="shrink-0" aria-hidden="true" />
    </div>
    <p className="cyclotron-tagline">SELF-ALIGNING DECISION MODEL HARNESS</p>
  </div>
}
