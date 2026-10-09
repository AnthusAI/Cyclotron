import {Input} from '@/components/ui/input'
import {Label} from '@/components/ui/label'
import {NativeSelect,NativeSelectOption} from '@/components/ui/native-select'

const objectives=['recall','precision','accuracy','f1','balanced_accuracy','brier','balanced_brier']
const cadenceFields=[['rubric_changes_every','Rubric: label changes per trigger'],['optimize_every','Other optimization stages: labels per trigger']] as const
const textFields=[['decisions_model','Decision model identifier'],['optimizer_model','Optimizer model identifier'],['seed','Partition seed']] as const
type Settings=Record<string,unknown>
type Policy={primary?:string;secondary?:string|null;aggregation?:string;max_secondary_regression?:number;minimum_secondary?:number|null}
export function validCyclotronSettings(settings:Settings){
  if('optimizer_transport' in settings&&!['openai','litellm'].includes(String(settings.optimizer_transport)))return false
  for(const [key] of cadenceFields)if(key in settings&&(!Number.isSafeInteger(settings[key])||Number(settings[key])<1))return false
  const policy=settings.selection_policy as Policy|undefined
  if(policy){
    if(!objectives.includes(policy.primary??'')||(policy.secondary&&(!objectives.includes(policy.secondary)||policy.secondary===policy.primary)))return false
    if(policy.max_secondary_regression!=null&&(!Number.isFinite(policy.max_secondary_regression)||policy.max_secondary_regression<0))return false
    if(policy.minimum_secondary!=null&&(!policy.secondary||['brier','balanced_brier'].includes(policy.secondary)||!Number.isFinite(policy.minimum_secondary)||policy.minimum_secondary<0||policy.minimum_secondary>1))return false
  }
  return true
}

export function CyclotronSettings({settings,onChange,disabled}:{settings:Settings;onChange:(value:Settings)=>void;disabled:boolean}){
  const policy=settings.selection_policy as Policy|undefined
  const provider=String(settings.decisions_provider??'')
  const changeProvider=(value:string)=>{
    const next={...settings}
    if(value)next.decisions_provider=value;else delete next.decisions_provider
    if((provider||'jev')!==(value||'jev'))delete next.decisions_model
    onChange(next)
  }
  const update=(key:string,value:unknown)=>{const next={...settings};if(value===undefined)delete next[key];else next[key]=value;onChange(next)}
  const updatePolicy=(change:Partial<Policy>)=>update('selection_policy',{aggregation:'macro',...policy,primary:policy?.primary??'f1',...change})
  return <fieldset className="space-y-3 rounded-md border p-3" disabled={disabled}>
    <legend className="px-1 text-sm font-semibold">Shared run defaults</legend>
    <p className="text-xs text-muted-foreground">Blank values use application defaults. New runs pin these settings. Existing sessions do not change.</p>
    <div className="space-y-1"><Label htmlFor="cyclotron-decisions_provider">Decision provider</Label><NativeSelect id="cyclotron-decisions_provider" value={provider} onChange={e=>changeProvider(e.target.value)}><NativeSelectOption value="">Application default (Jev)</NativeSelectOption><NativeSelectOption value="jev">Jev</NativeSelectOption><NativeSelectOption value="kev">Kev (local server)</NativeSelectOption><NativeSelectOption value="laya">Laya (local checkpoint)</NativeSelectOption>{provider&&!['jev','kev','laya'].includes(provider)?<NativeSelectOption value={provider}>{provider} (custom host required)</NativeSelectOption>:null}</NativeSelect></div>
    <p className="text-xs text-muted-foreground">Changing provider clears the model identifier so its default can apply. Kev uses the local server on port 8009. Laya requires the optional local model installation and rejects context that exceeds its checkpoint budget. Example effectiveness must be measured; providers never fall back to Jev.</p>
    <div className="space-y-1"><Label htmlFor="cyclotron-optimizer_transport">Optimizer transport</Label><NativeSelect id="cyclotron-optimizer_transport" value={String(settings.optimizer_transport??'')} onChange={e=>update('optimizer_transport',e.target.value||undefined)}><NativeSelectOption value="">Application default (OpenAI)</NativeSelectOption><NativeSelectOption value="openai">OpenAI</NativeSelectOption><NativeSelectOption value="litellm">LiteLLM (multiple providers)</NativeSelectOption></NativeSelect></div>
    <p className="text-xs text-muted-foreground">LiteLLM requires the optional litellm-optimizer installation and a provider-qualified model identifier, such as anthropic/your-model or ollama/your-model. Provider credentials remain on the server. Unsupported JSON output fails explicitly; no automatic paid retry.</p>
    <div className="grid gap-3 sm:grid-cols-2">{textFields.map(([key,label])=><div key={key} className="space-y-1"><Label htmlFor={`cyclotron-${key}`}>{label}</Label><Input id={`cyclotron-${key}`} value={String(settings[key]??'')} onChange={e=>update(key,e.target.value.trim()||undefined)}/></div>)}
    {cadenceFields.map(([key,label])=><div key={key} className="space-y-1"><Label htmlFor={`cyclotron-${key}`}>{label}</Label><Input id={`cyclotron-${key}`} type="number" min="1" step="1" value={String(settings[key]??'')} onChange={e=>update(key,e.target.value===''?undefined:Number(e.target.value))}/></div>)}</div>
    <p className="text-xs text-muted-foreground">Only learning-eligible labels count toward triggers. Each classifier keeps its own cadence.</p>
    <div className="grid gap-3 sm:grid-cols-2"><div className="space-y-1"><Label htmlFor="cyclotron-primary">Primary objective</Label><NativeSelect id="cyclotron-primary" value={policy?.primary??''} onChange={e=>{if(e.target.value)updatePolicy({primary:e.target.value,...(policy?.secondary===e.target.value?{secondary:null,minimum_secondary:null}:{})});else update('selection_policy',undefined)}}><NativeSelectOption value="">Application default</NativeSelectOption>{objectives.map(metric=><NativeSelectOption key={metric} value={metric}>{metric.replaceAll('_',' ')}</NativeSelectOption>)}</NativeSelect></div>
    <div className="space-y-1"><Label htmlFor="cyclotron-secondary">Secondary objective</Label><NativeSelect id="cyclotron-secondary" value={policy?.secondary??''} onChange={e=>updatePolicy({secondary:e.target.value||null,minimum_secondary:null})}><NativeSelectOption value="">None</NativeSelectOption>{objectives.filter(metric=>metric!==policy?.primary).map(metric=><NativeSelectOption key={metric} value={metric}>{metric.replaceAll('_',' ')}</NativeSelectOption>)}</NativeSelect></div>
    <div className="space-y-1"><Label htmlFor="cyclotron-regression">Maximum secondary regression (0–1 scale)</Label><Input id="cyclotron-regression" type="number" min="0" step="0.01" value={policy?.max_secondary_regression??''} onChange={e=>updatePolicy({max_secondary_regression:e.target.value===''?0:Number(e.target.value)})}/></div>
    <div className="space-y-1"><Label htmlFor="cyclotron-floor">Minimum secondary score (0–1)</Label><Input id="cyclotron-floor" type="number" min="0" max="1" step="0.01" disabled={!policy?.secondary||['brier','balanced_brier'].includes(policy.secondary)} value={policy?.minimum_secondary??''} onChange={e=>updatePolicy({minimum_secondary:e.target.value===''?null:Number(e.target.value)})}/></div></div>
    <p className="text-xs text-muted-foreground">Recall, precision, and F1 use each classifier’s configured positive class, or macro averaging when none is designated. Classifier-specific objective settings take precedence. Brier objectives are minimized; other objectives are maximized.</p>
    {!validCyclotronSettings(settings)?<p role="alert" className="text-xs text-destructive">Use positive whole-number cadences, different objectives, and valid secondary limits before saving.</p>:null}
  </fieldset>
}
