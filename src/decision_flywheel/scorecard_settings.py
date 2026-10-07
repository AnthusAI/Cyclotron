"""Validate executable shared defaults without discarding extension metadata."""
from .selection_policy import SelectionPolicy


def validate_shared_settings(settings):
    if not isinstance(settings,dict):raise ValueError('shared settings must be structured')
    for key in ('optimize_every','rubric_changes_every'):
        if key in settings and (type(settings[key]) is not int or settings[key]<1):
            raise ValueError(f'{key} must be a positive integer')
    for key in ('seed','decisions_model','optimizer_model','decisions_provider'):
        if key in settings and (not isinstance(settings[key],str) or not settings[key].strip()):
            raise ValueError(f'{key} must be a nonempty identifier')
    if 'selection_policy' in settings:
        policy=settings['selection_policy']
        if not isinstance(policy,dict):raise ValueError('selection policy must be structured')
        # Class roles are resolved separately for each pinned classifier at
        # run creation. A shared policy cannot require one common label name.
        try:SelectionPolicy(**{**policy,'aggregation':'macro','positive_class':None})
        except TypeError as error:raise ValueError('unknown selection policy field') from error
    return dict(settings)
