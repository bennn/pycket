import pytest
from pycket.expand import expand, to_ast
from pycket.interpreter import *
from pycket.values import *
from pycket.prims import *

from pycket.test.test_basic import run_top

def run_file(fname):
    with file(fname) as f:
        s = f.read()
    return run_top(s, w_void)

def test_puzzle():
    assert run_file("puzzle.sch")
    
        
