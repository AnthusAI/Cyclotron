import pytest

from .trace_server import viewer_response, bind_address


def test_the_viewer_defaults_to_loopback_and_allows_an_explicit_local_network_address():
    assert bind_address() == '127.0.0.1'
    assert bind_address('192.168.0.199') == '192.168.0.199'
    for host in ['0.0.0.0', '8.8.8.8', 'example.com']:
        with pytest.raises(ValueError):
            bind_address(host)


def test_an_unspecified_bind_requires_an_explicit_opt_in():
    assert bind_address('0.0.0.0',allow_unspecified=True) == '0.0.0.0'


def test_only_the_selected_viewer_is_served_not_neighboring_private_files(tmp_path):
    viewer=tmp_path/'viewer.html'
    viewer.write_text('<h1>Trace</h1>')
    (tmp_path/'credentials.env').write_text('private')
    assert viewer_response(viewer,'/')[0]==200
    assert viewer_response(viewer,'/index.html')[2]==viewer.read_bytes()
    for url in ['/credentials.env','/../credentials.env','/runtime.sqlite3']:
        assert viewer_response(viewer,url)[0]==404
