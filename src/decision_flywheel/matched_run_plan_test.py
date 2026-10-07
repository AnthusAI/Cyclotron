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


def test_normalized_duplicate_learning_content_is_excluded_even_after_retraction():
    before,after=source('before'),source('after')
    for endpoint in (before,after):
        endpoint['items'][0]['values']={'title':'Caf\u00e9','text':'one two'}
    before['items'].append({'id':'learned-copy','revision':1,'fingerprint':'different-id','values':{'title':'Cafe\u0301','text':' one\n  two '}})
    before['events'].extend([
        {'sequence':5,'payload':{'kind':'human-feedback','classifier_id':'topic','action':'submitted','assignment':'training','feedback':{'item_id':'learned-copy','final_answer_value':'no'}}},
        {'sequence':6,'payload':{'kind':'human-feedback','classifier_id':'topic','action':'retracted','feedback':{'item_id':'learned-copy','final_answer_value':'no'}}}])
    plan=plan_matched_runs(before,after)
    assert '0' not in plan['item_ids']
    assert plan['sample_count']==3
    assert plan['content_exclusion_policy']=='unicode-nfc-and-whitespace-normalized-structured-values-v1'


def test_missing_previously_learned_item_content_cannot_be_claimed_clean():
    before,after=source('before'),source('after')
    before['events'].append({'sequence':5,'payload':{'kind':'human-feedback','classifier_id':'topic','action':'submitted','assignment':'training','feedback':{'item_id':'missing','final_answer_value':'yes'}}})
    # The other endpoint's version is not evidence of what the first one learned.
    after['items'].append({'id':'missing','revision':1,'fingerprint':'missing','values':{'text':'some other content'}})
    with pytest.raises(ValueError,match='learnable item content'):
        plan_matched_runs(before,after)


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
