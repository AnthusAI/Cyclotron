"""Descriptive snapshots from recorded predictions; no fitting or rescoring."""
from collections import OrderedDict, Counter
from .rolling_metrics import recent_reviewed_metrics
from .calibration_metrics import reliability_curve
from .output_comparison import compare_outputs


def reviewed_calibration_metrics(classes, events, *, limit=200):
    classes=tuple(classes)
    predictions={}; reviewed=OrderedDict()
    for event in events:
        if event['kind']=='prediction':
            predictions[event['target_id']]=event
        elif event['kind']=='human-feedback':
            feedback=event['feedback']; identifier=feedback['item_id']
            reviewed.pop(identifier,None)
            if event.get('action')!='retracted' and identifier in predictions:
                reviewed[identifier]=(feedback['final_answer_value'],predictions[identifier],event.get('event_id'))
    metrics=recent_reviewed_metrics(classes,[(identifier,actual,prediction['label'],prediction.get('probabilities'))
                                          for identifier,(actual,prediction,_) in reviewed.items()],limit=limit)
    samples=[]; sources=Counter(); versions=Counter();raw=[];calibrated=[];truth=[]
    for identifier,(actual,prediction,feedback_id) in list(reviewed.items())[-limit:]:
        source='calibrated-head' if prediction.get('fitted_head') else 'decision-passthrough'
        sources[source]+=1;versions[prediction.get('version','unrecorded')]+=1
        samples.append({'item_id':identifier,'prediction_event_id':prediction.get('event_id'),
                        'feedback_event_id':feedback_id,'version':prediction.get('version'),
                        'source':source,'temperature':prediction.get('calibration_temperature'),
                        'calibration_provenance':prediction.get('calibration_provenance')})
        if prediction.get('fitted_head') and prediction.get('uncalibrated_probabilities') and prediction.get('probabilities'):
            truth.append(actual);raw.append(prediction['uncalibrated_probabilities']);calibrated.append(prediction['probabilities'])
    curve=metrics['calibration']
    curve.update(samples=samples,source_counts=dict(sources),version_counts=dict(versions),
                 scope='latest unique human-reviewed pre-vote predictions; mixed historical versions; not held-out')
    if raw:
        curve['matched_head_comparison']={
            'raw':reliability_curve(classes,truth,[max(classes,key=row.get) for row in raw],raw),
            'calibrated':reliability_curve(classes,truth,[max(classes,key=row.get) for row in calibrated],calibrated),
            'scope':'matched fitted-head samples only; raw and calibrated use their own top labels'}
    window=list(reviewed.items())[-limit:]
    metrics['decision_model_comparison']=compare_outputs(classes,
        [{**prediction,'item_id':identifier,'actual_label':actual} for identifier,(actual,prediction,_) in window],limit=limit)
    return metrics
