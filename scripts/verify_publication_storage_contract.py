"""Run a trusted local publication contract against THIS checkout's Python core.

Reads the integration owner's module/specs without copying or modifying its
worktree. All engine data is disposable; clients use offline/ASGI fixtures.
This proves local cross-branch compatibility, not deployed auth or cloud storage.
"""
import argparse
import importlib.util
from pathlib import Path
import sys
import tempfile


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('publication_checkout',type=Path,help='trusted local integration-owner checkout')
    arguments=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    sys.path.insert(0,str(root/'src'))
    sys.dont_write_bytecode=True
    import decision_flywheel
    if Path(decision_flywheel.__file__).resolve().parent!=root/'src'/'decision_flywheel':
        raise RuntimeError('wrong core checkout imported')
    external=arguments.publication_checkout.resolve()/'src'/'decision_flywheel'
    for name in ('publication_lab','publication_lab_test'):
        path=external/(name+'.py')
        if not path.is_file(): raise ValueError('publication implementation and contract specifications required')
        module_name='decision_flywheel.'+name
        spec=importlib.util.spec_from_file_location(module_name,path)
        module=importlib.util.module_from_spec(spec)
        sys.modules[module_name]=module
        spec.loader.exec_module(module)
    from decision_flywheel import embedded_cyclotron,flywheel
    for module in (embedded_cyclotron,flywheel):
        if Path(module.__file__).resolve().parent!=root/'src'/'decision_flywheel':
            raise RuntimeError('publication specifications imported a different engine')
    import pytest
    print('Testing external publication contract with current storage/core checkout.')
    with tempfile.TemporaryDirectory(prefix='cyclotron-publication-storage-') as directory:
        return pytest.main([str(external/'publication_lab_test.py'),'-q','--import-mode=importlib',
            '-p','no:cacheprovider','--basetemp='+directory])


if __name__=='__main__': raise SystemExit(main())
