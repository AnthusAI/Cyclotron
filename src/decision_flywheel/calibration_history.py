"""Descriptive snapshots from recorded predictions; no fitting or rescoring."""
from collections import OrderedDict, Counter
from .rolling_metrics import recent_reviewed_metrics
from .calibration_metrics import reliability_curve


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
    paired=[(identifier,actual,prediction) for identifier,(actual,prediction,_) in window
            if prediction.get('decision_model_label') in classes]
    metrics['decision_model_comparison']={
        'count':len(paired),'missing_raw_count':len(window)-len(paired),
        'item_ids':[identifier for identifier,_,_ in paired],
        'scope':'same latest reviewed pre-vote items; descriptive, not held-out',
        'raw':recent_reviewed_metrics(classes,[(identifier,actual,p['decision_model_label'],p.get('decision_model_probabilities'))
                                             for identifier,actual,p in paired],limit=limit),
        'final':recent_reviewed_metrics(classes,[(identifier,actual,p['label'],p.get('probabilities'))
                                               for identifier,actual,p in paired],limit=limit)}
    comparison=metrics['decision_model_comparison']
    paired_probabilities=[bool(p.get('decision_model_probabilities') and p.get('probabilities')) for _,_,p in paired]
    comparison['missing_paired_probability_count']=sum(not eligible for eligible in paired_probabilities)
    for side,label_key,probability_key in (('raw','decision_model_label','decision_model_probabilities'),('final','label','probabilities')):
        comparison[side]['calibration']=reliability_curve(classes,[actual for _,actual,_ in paired],
            [p[label_key] for _,_,p in paired],
            [p.get(probability_key) if eligible else None for (_,_,p),eligible in zip(paired,paired_probabilities)])
    return metrics
