"""Read-only, directional differences between immutable cyclotron definitions."""


def compare_definitions(before, after):
    if before['id']!=after['id']:
        raise ValueError('compare revisions of the same cyclotron')
    positions=lambda definition:{row['id']:{'position':index+1,'revision':row['revision']}
        for index,row in enumerate(definition['classifiers'])}
    old,new=positions(before),positions(after)
    identifiers=list(dict.fromkeys([*old,*new]))
    old_settings,new_settings=before.get('settings',{}),after.get('settings',{})
    return {'name':None if before['name']==after['name'] else {'before':before['name'],'after':after['name']},
        'members':[{'id':identifier,'before':old.get(identifier),'after':new.get(identifier)}
                   for identifier in identifiers if old.get(identifier)!=new.get(identifier)],
        'settings':[{'key':key,'before_present':key in old_settings,'before':old_settings.get(key),
                     'after_present':key in new_settings,'after':new_settings.get(key)}
                    for key in sorted(set(old_settings)|set(new_settings))
                    if (key in old_settings,old_settings.get(key))!=(key in new_settings,new_settings.get(key))]}
