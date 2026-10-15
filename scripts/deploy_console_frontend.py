"""Deploy only built existing console assets through normal Amplify manual hosting.

Fixed approved account/app; no public source push or Git-provider credential.
Presigned upload URLs stay in memory and are never printed or persisted.
"""
import io
import json
from pathlib import Path
import subprocess
import zipfile
import httpx

APP='d2lya5yok0779c'
def aws(*args):
    result=subprocess.run(['aws',*args,'--profile','cyclotron-production','--region','us-east-1','--output','json','--no-cli-pager'],capture_output=True,text=True)
    if result.returncode:raise RuntimeError('Amplify deployment action failed; inspect scoped AWS action separately')
    return json.loads(result.stdout)

def main():
    assert aws('sts','get-caller-identity')['Account']=='682849160744'
    app=aws('amplify','get-app','--app-id',APP)['app']
    assert app['name']=='cyclotron-console' and app['platform']=='WEB'
    root=Path(__file__).resolve().parents[1]/'trace-ui/console-dist'
    assert (root/'index.html').is_file()
    data=io.BytesIO()
    with zipfile.ZipFile(data,'w',zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(root.rglob('*')):
            if path.is_file():bundle.write(path,path.relative_to(root).as_posix())
    deployment=aws('amplify','create-deployment','--app-id',APP,'--branch-name','production')
    response=httpx.put(deployment['zipUploadUrl'],content=data.getvalue(),timeout=60,follow_redirects=False)
    if response.status_code not in (200,201):raise RuntimeError('Amplify artifact upload rejected')
    result=aws('amplify','start-deployment','--app-id',APP,'--branch-name','production','--job-id',deployment['jobId'])
    print(json.dumps({'appId':APP,'jobId':result['jobSummary']['jobId'],'status':result['jobSummary']['status'],'sourcePushed':False}))
if __name__=='__main__':main()
