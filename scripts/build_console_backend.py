"""Build the Linux Lambda zip locally; never uploads, deploys or obtains creds."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
STAGING=ROOT/'deployment/.build/backend'
LOCK=ROOT/'deployment/requirements-linux-py313.lock'


def build(reuse=False):
    if not reuse:
        if STAGING.exists():shutil.rmtree(STAGING)
        STAGING.mkdir(parents=True)
        subprocess.run([sys.executable,'-m','pip','install','--disable-pip-version-check','--target',str(STAGING),
            '--platform','manylinux_2_34_x86_64','--platform','manylinux_2_28_x86_64','--platform','manylinux2014_x86_64',
            '--python-version','3.13','--implementation','cp','--abi','cp313','--only-binary=:all:',
            '--no-compile','--require-hashes','-r',str(LOCK)],check=True)
    elif not (STAGING/'mangum').exists():raise ValueError('dependency staging absent')
    target=STAGING/'decision_flywheel'
    if target.exists():shutil.rmtree(target)
    shutil.copytree(ROOT/'src/decision_flywheel',target,ignore=shutil.ignore_patterns('__pycache__','*_test.py','*.pyc','*.feature'))
    license=ROOT/'LICENSE'
    if license.exists():shutil.copyfile(license,STAGING/'LICENSE.decision-flywheel')
    archive=ROOT/'deployment/console-backend.zip';size=0;files=0
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=9) as output:
        for path in sorted(STAGING.rglob('*')):
            if not path.is_file() or '__pycache__' in path.parts or path.suffix=='.pyc' or path.relative_to(STAGING).parts[0]=='bin':continue
            if path.is_symlink():raise ValueError('unexpected deployment symlink')
            body=path.read_bytes();size+=len(body);files+=1
            info=zipfile.ZipInfo(path.relative_to(STAGING).as_posix(),date_time=(2026,1,1,0,0,0))
            info.external_attr=0o100644<<16;info.compress_type=zipfile.ZIP_DEFLATED
            output.writestr(info,body)
    if size>240*1024*1024:raise ValueError('artifact exceeds conservative uncompressed Lambda limit')
    manifest={'runtime':'python3.13','architecture':'x86_64','files':files,'uncompressedBytes':size,
        'zipBytes':archive.stat().st_size,'sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),
        'lockSha256':hashlib.sha256(LOCK.read_bytes()).hexdigest(),'cloudResourcesChanged':False}
    (ROOT/'deployment/console-backend.manifest.json').write_text(json.dumps(manifest,indent=2)+'\n')
    print(json.dumps(manifest))

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reuse-dependencies',action='store_true',help='Package the dependency staging just installed with the matching pinned wheel lock')
    build(parser.parse_args().reuse_dependencies)
