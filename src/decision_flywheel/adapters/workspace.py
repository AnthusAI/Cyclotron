"""Explicit, lazy provider construction for the optional web application."""
from ..decision_provider_settings import normalize_decision_settings


def decision_adapter(config):
    settings=normalize_decision_settings(config)
    provider,model=settings['decisions_provider'],settings['decisions_model']
    if provider=='jev':
        from .jev import JevAdapter,JevConfiguration
        return JevAdapter.from_environment(configuration=JevConfiguration(model=model))
    if provider=='kev':
        from .kev import KevAdapter,KevConfiguration
        return KevAdapter(configuration=KevConfiguration(model=model))
    if provider=='laya':
        raise ValueError('Laya workspace optimization is unsupported: the adapter lacks a verified full-context scorecard protocol')
    raise ValueError('unsupported decision provider; inject an application model factory for custom providers')
