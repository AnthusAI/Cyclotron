from .trace_server import viewer_response


def test_only_the_selected_viewer_is_served_not_neighboring_private_files(tmp_path):
    viewer=tmp_path/'viewer.html'
    viewer.write_text('<h1>Trace</h1>')
    (tmp_path/'credentials.env').write_text('private')
    assert viewer_response(viewer,'/')[0]==200
    assert viewer_response(viewer,'/index.html')[2]==viewer.read_bytes()
    for url in ['/credentials.env','/../credentials.env','/runtime.sqlite3']:
        assert viewer_response(viewer,url)[0]==404
