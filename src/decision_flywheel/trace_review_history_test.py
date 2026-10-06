import sqlite3
from .trace_review_history import read_review_history


def test_original_votes_predictions_and_source_provenance_are_preserved_read_only(tmp_path):
    path=tmp_path/'reviews.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE articles(id,title,abstract,assignment); CREATE TABLE presentations(id,article_id,predicted_label,confidence,shown_at); CREATE TABLE review_events(id,article_id,action,label,comment,created_at,presentation_id);')
        db.execute('INSERT INTO articles VALUES (?,?,?,?)',('a','Title','Abstract','train'))
        db.execute('INSERT INTO presentations VALUES (?,?,?,?,?)',('p','a','exclude',.8,'2026-10-06T12:00:00Z'))
        db.execute('INSERT INTO review_events VALUES (?,?,?,?,?,?,?)',('v','a','vote','include','why','2026-10-06T12:01:00Z','p'))
    before=path.read_bytes()
    rows=read_review_history(path)
    assert [r['source_table'] for r in rows]==['presentations','review_events']
    assert rows[1]['record']['comment']=='why'
    assert rows[1]['presentation']['predicted_label']=='exclude'
    assert path.read_bytes()==before
