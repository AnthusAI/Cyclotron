"""Print exact same-origin Amplify reverse-proxy rules; never updates an app."""
import argparse
import json
import re

def rules(origin):
    if not re.fullmatch(r'https://[a-z0-9]+\.execute-api\.us-east-1\.amazonaws\.com',origin):raise ValueError('reviewed regional HTTP API origin required')
    return [dict(source=path,target=origin+path,status='200') for path in ('/auth/<*>','/api/<*>','/graphql','/runs/<*>')]+[
        dict(source='</^[^.]+$|\\.(?!(css|gif|ico|jpg|js|png|txt|svg|woff|woff2|ttf|map|json|webp)$)([^.]+$)/>',target='/index.html',status='200')]
if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('backend_origin')
    print(json.dumps(rules(parser.parse_args().backend_origin),indent=2))
