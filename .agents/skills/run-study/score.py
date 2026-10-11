"""Score a finished or crashed study from its runtime event log, against the corpus reference labels.
usage: score.py RUN_DIR/MODE CORPUS.jsonl [--window 100]
Needs no keys and makes no calls. Prints per-window accuracy of Cyclotron, the decision model alone as the
running classifier saw it, and the most-common-label baseline, plus the cycles where a rubric became active."""
import json, sqlite3, sys
path, corpus = sys.argv[1], sys.argv[2]
size = int(sys.argv[sys.argv.index('--window') + 1]) if '--window' in sys.argv else 100
label = {r['id']: r['simulated_label'] for r in map(json.loads, open(corpus))}
events = [json.loads(p) for (p,) in sqlite3.connect(f'{path}/runtime.sqlite3').execute('select payload from runtime_events order by id')]
pred = {e['cycle_number']: e for e in events if e['kind'] == 'prediction'}
active = sorted({e['cycle_number'] for e in events if e['kind'] == 'classifier-activated'})
n = len(pred)
print(f'{n} cycles; classifier activations at cycles {active}')
print('window      cyclotron  most-common-label')
for a in range(1, n + 1, size):
    cs = [c for c in range(a, min(a + size, n + 1))]
    right = sum(pred[c]['label'] == label[pred[c]['cycle_item_id']] for c in cs)
    common = sum(label[pred[c]['cycle_item_id']] == 'reject' for c in cs)
    print(f'{cs[0]}-{cs[-1]:<6} {100*right/len(cs):8.1f}%  {100*common/len(cs):8.1f}%')
print(f'overall     {100*sum(pred[c]["label"] == label[pred[c]["cycle_item_id"]] for c in pred)/n:8.1f}%')
