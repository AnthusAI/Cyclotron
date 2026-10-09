from .definition_diff import compare_definitions


def test_comparison_preserves_direction_order_and_all_shared_setting_changes():
    before={'id':'card','revision':2,'name':'Old','classifiers':[{'id':'a','revision':1},{'id':'b','revision':1}], 'settings':{'model':'old','removed':None}}
    after={'id':'card','revision':3,'name':'New','classifiers':[{'id':'b','revision':2},{'id':'c','revision':1}], 'settings':{'model':'new','added':None}}
    result=compare_definitions(before,after)
    assert result['name']=={'before':'Old','after':'New'}
    assert result['members']==[
        {'id':'a','before':{'position':1,'revision':1},'after':None},
        {'id':'b','before':{'position':2,'revision':1},'after':{'position':1,'revision':2}},
        {'id':'c','before':None,'after':{'position':2,'revision':1}}]
    assert result['settings']==[
        {'key':'added','before_present':False,'before':None,'after_present':True,'after':None},
        {'key':'model','before_present':True,'before':'old','after_present':True,'after':'new'},
        {'key':'removed','before_present':True,'before':None,'after_present':False,'after':None}]
    assert before['name']=='Old'


def test_unchanged_definitions_have_no_changes_and_different_identities_cannot_be_compared():
    import pytest
    row={'id':'one','revision':1,'name':'Card','classifiers':[],'settings':{}}
    assert compare_definitions(row,row)=={'name':None,'members':[],'settings':[]}
    with pytest.raises(ValueError,match='same cyclotron'):
        compare_definitions(row,{**row,'id':'two'})
