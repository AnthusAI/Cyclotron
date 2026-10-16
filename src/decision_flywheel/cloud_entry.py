"""Standard API Gateway v2 and SQS Lambda entry points; no work on import."""
import asyncio
import json
import os

_cached_runtime=None
_cached_adapter=None


def runtime():
    global _cached_runtime
    if _cached_runtime is None:
        from botocore.config import Config
        from botocore.session import get_session
        from .hosted_runtime import HostedConfig,HostedRuntime
        config=HostedConfig.from_environment(os.environ)
        session=get_session()
        settings=Config(signature_version='s3v4',retries={'total_max_attempts':1},connect_timeout=5,read_timeout=10)
        s3=session.create_client('s3',region_name=config.region,config=settings)
        kms=session.create_client('kms',region_name=config.region,config=Config(retries={'total_max_attempts':1},connect_timeout=5,read_timeout=10))
        sqs=session.create_client('sqs',region_name=config.region)
        _cached_runtime=HostedRuntime(config,s3=s3,kms=kms,sqs=sqs)
    return _cached_runtime


def bff_handler(event,context):
    from .prospectus_accounts import account_request_id
    global _cached_adapter
    if _cached_adapter is None:
        from mangum import Mangum
        # Clients belong to the warm function, not each request's ASGI lifespan.
        _cached_adapter=Mangum(runtime().app(),lifespan='off')
    try:loop=asyncio.get_event_loop()
    except RuntimeError:loop=None
    if loop is None or loop.is_closed():asyncio.set_event_loop(asyncio.new_event_loop())
    correlation = account_request_id.set(getattr(context, 'aws_request_id', None))
    try:
        return _cached_adapter(event,context)
    finally:
        account_request_id.reset(correlation)


def worker_handler(event,context):
    if not isinstance(event,dict) or not isinstance(event.get('Records'),list) or len(event['Records'])>10:
        raise ValueError('bounded SQS batch required')
    async def process():
        failures=[]
        for record in event['Records']:
            identifier=record.get('messageId')
            if not isinstance(identifier,str) or not 1<=len(identifier)<=128:raise ValueError('SQS delivery identity required')
            try:
                body=record.get('body')
                if not isinstance(body,str) or len(body.encode())>4096:raise ValueError('bounded queue references required')
                await runtime().worker().process(json.loads(body))
            except Exception:
                # No token, raw payload, private model error or stack is logged.
                # Uncertain/running claims retry only into fail-closed diagnosis
                # and eventually the DLQ; they never grant another paid dispatch.
                failures.append({'itemIdentifier':identifier})
        return {'batchItemFailures':failures}
    return asyncio.run(process())
