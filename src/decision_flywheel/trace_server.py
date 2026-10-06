"""Serve one private trace viewer on loopback, never its neighboring files."""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def viewer_response(viewer, url):
    if urlsplit(url).path not in {'/', '/index.html'}:
        return 404, 'text/plain; charset=utf-8', b'Not found'
    return 200, 'text/html; charset=utf-8', Path(viewer).read_bytes()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--viewer',type=Path,required=True)
    parser.add_argument('--port',type=int,default=8780)
    args=parser.parse_args()
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

    server=ThreadingHTTPServer(('127.0.0.1',args.port),Handler)
    print(f'Private viewer: http://127.0.0.1:{args.port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__=='__main__':
    main()
