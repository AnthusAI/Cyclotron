"""Pure application settings; provider construction remains in adapters."""
DEFAULT_MODELS={'jev':'jev-1.13.0','kev':'kev-latest','laya':'convaiinnovations/laya'}


def normalize_decision_settings(values):
    values=dict(values)
    provider=values.setdefault('decisions_provider','jev')
    if not isinstance(provider,str) or not provider.strip():
        raise ValueError('decision provider must be a nonempty identifier')
    if 'decisions_model' not in values:
        if provider not in DEFAULT_MODELS:
            raise ValueError('custom decision provider requires an explicit model identifier')
        values['decisions_model']=DEFAULT_MODELS[provider]
    if not isinstance(values['decisions_model'],str) or not values['decisions_model'].strip():
        raise ValueError('decision model must be a nonempty identifier')
    return values
