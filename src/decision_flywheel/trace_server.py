"""Serve one trace viewer on loopback or an explicit LAN address, never neighboring files."""
import argparse
from ipaddress import IPv4Address
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def bind_address(host='127.0.0.1'):
    address = IPv4Address(host)
    if not (address.is_loopback or address.is_private) or address.is_unspecified or address.is_reserved:
        raise ValueError('use a loopback or specific private LAN IPv4 address')
    return str(address)


def viewer_response(viewer, url):
    if urlsplit(url).path not in {'/', '/index.html'}:
        return 404, 'text/plain; charset=utf-8', b'Not found'
    return 200, 'text/html; charset=utf-8', Path(viewer).read_bytes()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--viewer',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8780)
    parser.add_argument('--host',default='127.0.0.1',help='explicit private LAN IPv4 address for local-network access')
    args=parser.parse_args()
    try:
        host = bind_address(args.host)
    except ValueError as error:
        parser.error(str(error))
    if not args.viewer.is_file():
        parser.error('viewer must be an existing HTML file')

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            status,content_type,body=viewer_response(args.viewer,self.path)
            self.send_response(status)
            self.send_header('Content-Type',content_type)
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            self.send_header('Content-Length',str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self,*args):
            pass

    server=ThreadingHTTPServer((host,args.port),Handler)
    print(f'Viewer: http://{host}:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
