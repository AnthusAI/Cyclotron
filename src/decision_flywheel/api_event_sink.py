"""Acknowledged GraphQL trace ingestion for reusable engine observers."""
QUERY = '''mutation Ingest($run: ID!, $events: [TraceInput!]!) {
  ingestEvents(runId: $run, events: $events) { sequence }
}'''


class GraphQLTraceSink:
    def __init__(self, endpoint, run_id, *, token=None, transport=None, namespace='engine'):
        self.endpoint, self.run_id, self.token, self.transport = endpoint, run_id, token, transport
        self.namespace = namespace

    def __call__(self, event):
        if not isinstance(event.get('event_id'), int):
            raise ValueError('a trace event needs a stable engine event identity')
        return self.ingest(f"{self.namespace}:{event['event_id']}", event)

    def ingest(self, source_id, event):
        body = {'query':QUERY,'variables':{'run':self.run_id,
            'events':[{'sourceId':source_id,'payload':event}]}}
        response = self._post(body)
        if not response.get('data', {}).get('ingestEvents'):
            raise RuntimeError('trace API did not acknowledge ingestion')
        return response['data']['ingestEvents'][0]['sequence']

    def complete(self, summary):
        response = self._post({'query': 'mutation($run:ID!,$summary:JSON!){completeRun(runId:$run,summary:$summary){id}}',
            'variables': {'run':self.run_id, 'summary':summary}})
        if response.get('data', {}).get('completeRun', {}).get('id') != self.run_id:
            raise RuntimeError('trace API did not acknowledge completion')

    def _post(self, body):
        if self.transport:
            response = self.transport(body)
        else:
            import httpx
            try:
                headers = {'Authorization':f'Bearer {self.token}'} if self.token else {}
                result = httpx.post(self.endpoint, json=body, headers=headers, timeout=15, trust_env=False)
                result.raise_for_status()
                response = result.json()
            except Exception:
                raise RuntimeError('trace API did not acknowledge ingestion') from None
        if response.get('errors'):
            raise RuntimeError('trace API did not acknowledge ingestion')
        return response
