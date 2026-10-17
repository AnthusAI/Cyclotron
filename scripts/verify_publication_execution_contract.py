"""Run local publication integration against the storage owner's pinned core.

Only reads trusted local source; does not edit either checkout or build packages.
All tests use temporary stores and offline model/ASGI boundaries.
"""
import argparse
import importlib.util
import sys
import tempfile
from pathlib import Path


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('core_checkout',type=Path)
    parser.add_argument('papyrus_checkout',type=Path)
    args=parser.parse_args()
    integration=Path(__file__).resolve().parents[1]/'src'/'decision_flywheel'
    core=args.core_checkout.resolve()/'src'
    sys.path.insert(0,str(core));sys.path.insert(0,str(args.papyrus_checkout.resolve()/'src'))
    sys.dont_write_bytecode=True
    import decision_flywheel
    if Path(decision_flywheel.__file__).resolve().parent!=core/'decision_flywheel':raise RuntimeError('wrong execution owner core')
    for name in ('publication_lab','publication_records','publication_steering','publication_lab_test','publication_composition_test'):
        spec=importlib.util.spec_from_file_location('decision_flywheel.'+name,integration/(name+'.py'))
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    import pytest
    paths=[integration/'publication_host_test.py',integration/'publication_lab_test.py',integration/'publication_composition_test.py',
           args.papyrus_checkout.resolve()/'src'/'papyrus_content'/'publication_bff_test.py']
    print('Local cross-owner verification; no cloud authority or production activation.')
    with tempfile.TemporaryDirectory(prefix='publication-execution-') as directory:
        return pytest.main([*(str(path) for path in paths),'-q','--import-mode=importlib','-p','no:cacheprovider','--basetemp='+directory,'--tb=short'])


if __name__=='__main__':raise SystemExit(main())
