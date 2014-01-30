import pytest
from pycket.expand import expand, expand_file, to_ast
from pycket.json import loads
from pycket.interpreter import *
from pycket.values import *
from pycket.prims import *

from pycket.test.test_basic import run_top

def run_file(fname, *replacements):
    s = expand_file(fname)
    for replace, with_ in replacements:
        assert s.count(replace) == 1
        s = s.replace(replace, with_)
    e = loads(s)
    ast = to_ast(e)
    val = interpret([ast])


def test_puzzle():
    run_file("puzzle.sch", ("1048575", "460"), ("50", "1"))

def test_nqueens():
    run_file("nqueens.sch", ("10000", "1"))

def test_bubble():
    run_file("bubble.sch", ("10000", "100"))

#def test_pseudoknot():
#    run_file("nucleic2.sch")

def test_microkanren():
    run_file("microkanren.sch")

#def test_minikanren():
#    run_file("minikanren.sch")
