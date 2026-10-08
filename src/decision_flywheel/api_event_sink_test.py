"""A runner logs through the API, not through an artifact export."""
import pytest
from .api_event_sink import GraphQLTraceSink


def test_sink_sends_full_events_and_reuses_stable_identity_without_logging_credentials():
    calls = []
    def post(body):
        calls.append(body)
        return {'data': {'ingestEvents': [{'sequence':1}]}}
    sink = GraphQLTraceSink('http://localhost/graphql', 'run', token='secret', transport=post)
    event = {'event_id':3,'kind':'decision-request','state':{'examples':[{'label':'include','text':'Example'}]}}
    sink(event)
    sink(event)
    assert calls[0] == calls[1]
    assert calls[0]['variables']['events'][0]['payload'] == event
    assert 'secret' not in str(calls)


def test_a_failed_api_acknowledgement_is_not_silently_replaced_by_a_local_file():
    sink = GraphQLTraceSink('http://localhost/graphql', 'run', transport=lambda _: {'errors':[{'message':'failure'}]})
    with pytest.raises(RuntimeError, match='acknowledge'):
        sink({'event_id':1, 'kind':'optimizer-request'})


def test_completed_comparison_is_published_through_the_same_api():
    calls = []
    sink = GraphQLTraceSink('unused', 'run', transport=lambda body:
        calls.append(body) or {'data': {'completeRun': {'id':'run'}}})
    sink.complete({'comparison': {'before': {'accuracy':.5}, 'after': {'accuracy':.8}}})
    assert calls[0]['variables']['run'] == 'run'
    assert calls[0]['variables']['summary']['comparison']['after']['accuracy'] == .8


def test_buffered_sink_preserves_event_order_and_uses_one_bounded_ingestion_request():
    calls=[]
    sink=GraphQLTraceSink('unused','run',batch_size=3,transport=lambda body:
        calls.append(body) or {'data':{'ingestEvents':[{'sequence':4},{'sequence':5},{'sequence':6}]}})
    for source in ('first','second','third'):
        sink.ingest(source,{'kind':'cycle-metrics','source':source})
    assert len(calls)==1
    assert [row['sourceId'] for row in calls[0]['variables']['events']]==['first','second','third']
    assert [row['payload']['source'] for row in calls[0]['variables']['events']]==['first','second','third']
