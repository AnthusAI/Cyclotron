"""Acknowledged GraphQL trace ingestion for reusable engine observers."""
QUERY = '''mutation Ingest($run: ID!, $events: [TraceInput!]!) {
  ingestEvents(runId: $run, events: $events) { sequence }
}'''


class GraphQLTraceSink:
    def __init__(self, endpoint, run_id, *, token=None, transport=None, namespace='engine', batch_size=1):
        if type(batch_size) is not int or not 1 <= batch_size <= 200:
            raise ValueError('trace batch size must be between one and 200')
        self.endpoint, self.run_id, self.token, self.transport = endpoint, run_id, token, transport
        self.namespace = namespace
        self.batch_size, self.pending = batch_size, []

    def set_batch_size(self, batch_size):
        """Opt into bounded delivery before an observer starts emitting."""
        if type(batch_size) is not int or not 1 <= batch_size <= 200:
            raise ValueError('trace batch size must be between one and 200')
        if self.pending:
            raise ValueError('flush trace events before changing their batch size')
        self.batch_size = batch_size
        return self

    def __call__(self, event):
        if not isinstance(event.get('event_id'), int):
            raise ValueError('a trace event needs a stable engine event identity')
        return self.ingest(f"{self.namespace}:{event['event_id']}", event)

    def ingest(self, source_id, event):
        if self.batch_size > 1:
            self.pending.append({'sourceId':source_id,'payload':event})
            if len(self.pending) >= self.batch_size:
                return self.flush()
            return None
        return self._ingest([{'sourceId':source_id,'payload':event}])[0]

    def flush(self):
        """Durably publish buffered observer events in their original order."""
        if not self.pending:
            return []
        events = list(self.pending)
        sequences = self._ingest(events)
        del self.pending[:len(events)]
        return sequences

    def _ingest(self, events):
        body = {'query':QUERY,'variables':{'run':self.run_id,
            'events':events}}
        response = self._post(body)
        rows = response.get('data', {}).get('ingestEvents')
        if not rows or len(rows) != len(events):
            raise RuntimeError('trace API did not acknowledge ingestion')
        return [row['sequence'] for row in rows]

    def complete(self, summary):
        self.flush()
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
