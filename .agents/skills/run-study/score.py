"""Score a finished or crashed study from its runtime event log, against the corpus reference labels.
usage: score.py RUN_DIR/MODE CORPUS.jsonl [--window 100]
Needs no keys and makes no calls. Prints per-window accuracy of Cyclotron, the decision model alone as the
running classifier saw it, and the most-common-label baseline, plus the cycles where a rubric became active."""
import json
import sqlite3
import sys

path, corpus = sys.argv[1], sys.argv[2]
size = int(sys.argv[sys.argv.index('--window') + 1]) if '--window' in sys.argv else 100
label = {r['id']: r['simulated_label'] for r in map(json.loads, open(corpus))}
events = [json.loads(p) for (p,) in sqlite3.connect(f'{path}/runtime.sqlite3').execute('select payload from runtime_events order by id')]
pred = {e['cycle_number']: e for e in events if e['kind'] == 'prediction'}
active = sorted({e['cycle_number'] for e in events if e['kind'] == 'classifier-activated'})
n = len(pred)
print(f'{n} cycles; classifier activations at cycles {active}')
print('window      accuracy  most-common  publish-recall  publish-precision')
def stats(cs):
    tp = sum(pred[c]['label'] == 'publish' and label[pred[c]['cycle_item_id']] == 'publish' for c in cs)
    pos = sum(label[pred[c]['cycle_item_id']] == 'publish' for c in cs)
    called = sum(pred[c]['label'] == 'publish' for c in cs)
    return (100 * tp / pos if pos else float('nan'), 100 * tp / called if called else float('nan'))
for a in range(1, n + 1, size):
    cs = [c for c in range(a, min(a + size, n + 1))]
    right = sum(pred[c]['label'] == label[pred[c]['cycle_item_id']] for c in cs)
    common = sum(label[pred[c]['cycle_item_id']] == 'reject' for c in cs)
    rec, prec = stats(cs)
    print(f'{cs[0]}-{cs[-1]:<6} {100*right/len(cs):7.1f}%  {100*common/len(cs):9.1f}%  {rec:12.1f}%  {prec:15.1f}%')
rec, prec = stats(list(pred))
print(f'overall     {100*sum(pred[c]["label"] == label[pred[c]["cycle_item_id"]] for c in pred)/n:7.1f}%  {"":10}  {rec:12.1f}%  {prec:15.1f}%')
raw = sum(pred[c]['decision_model_label'] == label[pred[c]['cycle_item_id']] for c in pred)
print(f'raw decision-model answers alone (before the learned model): {100*raw/n:.1f}% accuracy')
