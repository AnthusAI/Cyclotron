import copy
import pytest
from .matched_run_plan import plan_matched_runs


def source(name):
    return {'id':name,'config':{'evaluation_protocol':'protected-feedback-v1','classifiers':[{'id':'topic','revision':1,'config':{'classes':[{'label':'yes','role':'positive'},{'label':'no','role':'negative'}]}}]},
        'checkpoint':{'fingerprint':'state-'+name,'payload':{'event_cursor':0,'classifiers':{}}},
        'items':[{'id':str(i),'revision':1,'fingerprint':str(i),'values':{'text':str(i)}} for i in range(4)],
        'events':[{'sequence':i+1,'payload':{'kind':'human-feedback','classifier_id':'topic','action':'submitted','assignment':'scoreboard','feedback':{'item_id':str(i),'final_answer_value':'yes' if i%2 else 'no'}}} for i in range(4)]}


def test_preflight_is_deterministic_read_only_and_freezes_matching_protected_items():
    before,after=source('before'),source('after');original=copy.deepcopy(before)
    plan=plan_matched_runs(before,after)
    assert plan==plan_matched_runs(before,after)
    assert before==original
    assert plan['sample_count']==4 and plan['request_upper_bound']==8
    assert plan['class_counts']=={'topic':{'yes':2,'no':2}}


def test_mismatched_items_labels_and_previously_learnable_votes_are_excluded():
    before,after=source('before'),source('after')
    after['items'][0]['revision']=2
    after['events'][1]['payload']['feedback']['final_answer_value']='no'
    before['events'][2]['payload']['assignment']='training'
    plan=plan_matched_runs(before,after)
    assert plan['item_ids']==['3']
    assert plan['excluded_count']==3


def test_unknown_historical_protection_protocol_is_not_claimed_clean():
    before,after=source('before'),source('after');before['config'].pop('evaluation_protocol')
    with pytest.raises(ValueError,match='protection protocol'):plan_matched_runs(before,after)


def test_checkpoints_without_a_recorded_event_boundary_cannot_be_guessed():
    before,after=source('before'),source('after');before['checkpoint']['payload'].pop('event_cursor')
    with pytest.raises(ValueError,match='event boundary'):plan_matched_runs(before,after)


def test_comparison_caps_the_shared_target_set_at_two_hundred():
    before,after=source('before'),source('after')
    for endpoint in (before,after):
        endpoint['items']=[{'id':str(i),'revision':1,'fingerprint':str(i),'values':{'text':str(i)}} for i in range(240)]
        endpoint['events']=[{'sequence':i+1,'payload':{'kind':'human-feedback','classifier_id':'topic','assignment':'scoreboard','action':'submitted','feedback':{'item_id':str(i),'final_answer_value':'yes' if i%2 else 'no'}}} for i in range(240)]
    plan=plan_matched_runs(before,after)
    assert plan['sample_count']==200 and plan['class_counts']['topic']=={'yes':100,'no':100}
