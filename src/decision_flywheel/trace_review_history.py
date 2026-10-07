"""Read actual reviewer source records; never synthesize flywheel events."""
from pathlib import Path
import sqlite3


def read_review_history(database):
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        articles={r['id']:dict(r) for r in db.execute('SELECT * FROM articles')}
        presentations={r['id']:dict(r) for r in db.execute('SELECT * FROM presentations')}
        rows=[]
        for row in presentations.values():
            rows.append({'source_table':'presentations','record':row,'article':articles[row['article_id']]})
        for raw in db.execute('SELECT * FROM review_events'):
            row=dict(raw)
            rows.append({'source_table':'review_events','record':row,'article':articles[row['article_id']],
                         'presentation':presentations.get(row.get('presentation_id'))})
        return sorted(rows,key=lambda r:r['record'].get('shown_at',r['record'].get('created_at','')))
