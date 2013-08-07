from contextlib import contextmanager
from ..compiler import compile
from ..types import (
    int8, int16, int32, int64, uint8, uint16, uint32, uint64,
    float32, float64, complex64, complex128
)
from .support import testcase, main

def caster(a):
    return a

@contextmanager
def assert_raise(exc):
    try:
        yield
    except exc, e:
        print e
    else:
        raise AssertionError('expecting exception: %s' % exc)

def caster_template(sty, dty, arg, exc):
    compiled = compile(caster, dty, [sty])
    with assert_raise(exc):
        compiled(arg)


@testcase
def test_signed_to_unsigned():
    caster_template(int32, uint32, -123, OverflowError)

@testcase
def test_signed_to_signed_truncated():
    caster_template(int32, int8, -256, OverflowError)

@testcase
def test_unsigned_to_signed():
    caster_template(uint8, int8, 128, OverflowError)

@testcase
def test_unsigned_to_signed_truncated():
    caster_template(uint16, int8, 0xffff, OverflowError)

if __name__ == '__main__':
    main()
