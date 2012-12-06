"""
This module contains the Pipeline class which provides a pluggable way to
define the transformations and the order in which they run on the AST.
"""

import inspect
import ast as ast_module
import logging
import functools
import pprint
from timeit import default_timer as _timer

import llvm.core as lc

import numba.closure
from numba import error
from numba import functions, naming, transforms, visitors
from numba import ast_type_inference as type_inference
from numba import ast_constant_folding as constant_folding
from numba import ast_translate
from numba import utils
from numba.utils import dump

from numba.minivect import minitypes

logger = logging.getLogger(__name__)

class Pipeline(object):
    """
    Runs a pipeline of transforms.
    """

    order = [
        'const_folding',
        'type_infer',
        'type_set',
        'closure_type_inference',
        'transform_for',
        'specialize',
        'late_specializer',
        'fix_ast_locations',
        'codegen',
    ]

    mixins = {}
    _current_pipeline_stage = None

    def __init__(self, context, func, ast, func_signature,
                 nopython=False, locals=None, order=None, codegen=False,
                 symtab=None, **kwargs):
        self.context = context
        self.func = func
        self.ast = ast
        self.func_signature = func_signature
        ast.pipeline = self

        self.func_name = kwargs.get('name')
        if not self.func_name:
            if func:
                module_name = inspect.getmodule(func).__name__
                name = '.'.join([module_name, func.__name__])
            else:
                name = ast.name

            self.func_name = naming.specialized_mangle(
                                    name, self.func_signature.args)

        self.symtab = symtab
        if symtab is None:
            self.symtab = {}

        # Let the pipeline create a module for the function it is compiling
        # and the user will link that in.
        assert 'llvm_module' not in kwargs
        self.llvm_module = lc.Module.new('tmp.module.%x' % id(func))

        self.nopython = nopython
        self.locals = locals
        self.kwargs = kwargs

        if order is None:
            self.order = list(Pipeline.order)
            if not codegen:
                self.order.remove('codegen')
        else:
            self.order = order

    def make_specializer(self, cls, ast, **kwds):
        "Create a visitor or transform and add any mixins"
        if self._current_pipeline_stage in self.mixins:
            before, after = self.mixins[self._current_pipeline_stage]
            classes = tuple(before + [cls] + after)
            name = '__'.join(cls.__name__ for cls in classes)
            cls = type(name, classes, {})

        assert 'llvm_module' not in kwds
        return cls(self.context, self.func, ast,
                   func_signature=self.func_signature, nopython=self.nopython,
                   symtab=self.symtab, func_name=self.func_name,
                   llvm_module=self.llvm_module, **kwds)

    def insert_specializer(self, name, after):
        "Insert a new transform or visitor into the pipeline"
        self.order.insert(self.order.index(after) + 1, name)

    def try_insert_specializer(self, name, after):
        if after in self.order:
            self.insert_specializer(name, after)

    @classmethod
    def add_mixin(cls, pipeline_stage, transform, before=False):
        before_mixins, after_mixins = cls.mixins.get(pipeline_stage, ([], []))
        if before:
            before_mixins.append(transform)
        else:
            after_mixins.append(transform)

        cls.mixins[pipeline_stage] = before_mixins, after_mixins

    def run_pipeline(self):
        # Uses a special logger for logging profiling information.
        logger = logging.getLogger("numba.pipeline.profiler")
        ast = self.ast
        talpha = _timer() # for profiling complete pipeline
        for method_name in self.order:
            ts = _timer() # for profiling individual stage
            if __debug__ and logger.getEffectiveLevel() < logging.DEBUG:
                stage_tuple = (method_name, utils.ast2tree(ast))
                logger.debug(pprint.pformat(stage_tuple))

            self._current_pipeline_stage = method_name
            ast = getattr(self, method_name)(ast)
            te = _timer() #  for profileing individual stage
            logger.info("%X pipeline stage %30s:\t%.3fms",
                        id(self), method_name, (te - ts) * 1000)
        tomega = _timer() # for profiling complete pipeline
        logger.info("%X pipeline entire:\t\t\t\t\t%.3fms",
                    id(self), (tomega - talpha) * 1000)
        return self.func_signature, self.symtab, ast

    #
    ### Pipeline stages
    #
    
    def const_folding(self, ast):
        const_marker = self.make_specializer(constant_folding.ConstantMarker,
                                             ast)
        const_marker.visit(ast)
        constvars = const_marker.get_constants()
        const_folder = self.make_specializer(constant_folding.ConstantFolder,
                                             ast, constvars=constvars)
        return const_folder.visit(ast)

    def type_infer(self, ast):
        type_inferer = self.make_specializer(
                    type_inference.TypeInferer, ast, locals=self.locals,
                    **self.kwargs)
        type_inferer.infer_types()

        self.func_signature = type_inferer.func_signature
        logger.debug("signature for %s: %s", self.func_name,
                     self.func_signature)
        self.symtab = type_inferer.symtab
        return ast

    def type_set(self, ast):
        visitor = self.make_specializer(type_inference.TypeSettingVisitor, ast)
        visitor.visit(ast)
        return ast

    def closure_type_inference(self, ast):
        type_inferer = self.make_specializer(
                            numba.closure.ClosureTypeInferer, ast)
        return type_inferer.visit(ast)

    def transform_for(self, ast):
        transform = self.make_specializer(transforms.TransformForIterable, ast)
        return transform.visit(ast)

    def specialize(self, ast):
        return ast

    def late_specializer(self, ast):
        specializer = self.make_specializer(transforms.LateSpecializer, ast)
        return specializer.visit(ast)

    def fix_ast_locations(self, ast):
        fixer = self.make_specializer(FixMissingLocations, ast)
        fixer.visit(ast)
        return ast

    def codegen(self, ast):
        self.translator = self.make_specializer(ast_translate.LLVMCodeGenerator,
                                                ast, **self.kwargs)
        self.translator.translate()
        return ast


class FixMissingLocations(visitors.NumbaVisitor):

    def __init__(self, context, func, ast, *args, **kwargs):
        super(FixMissingLocations, self).__init__(context, func, ast,
                                                  *args, **kwargs)
        self.lineno = getattr(ast, 'lineno', 1)
        self.col_offset = getattr(ast, 'col_offset', 0)

    def visit(self, node):
        if not hasattr(node, 'lineno'):
            node.lineno = self.lineno
            node.col_offset = self.col_offset

        super(FixMissingLocations, self).visit(node)

def run_pipeline(context, func, ast, func_signature,
                 pipeline=None, **kwargs):
    """
    Run a bunch of AST transformers and visitors on the AST.
    """
    # print __import__('ast').dump(ast)
    pipeline = pipeline or context.numba_pipeline(context, func, ast,
                                                  func_signature, **kwargs)
    return pipeline, pipeline.run_pipeline()

def _infer_types(context, func, restype=None, argtypes=None, **kwargs):
    ast = functions._get_ast(func)
    func_signature = minitypes.FunctionType(return_type=restype,
                                            args=argtypes)
    return run_pipeline(context, func, ast, func_signature, **kwargs)

def infer_types(context, func, restype=None, argtypes=None, **kwargs):
    """
    Like run_pipeline, but takes restype and argtypes instead of a FunctionType
    """
    pipeline, (sig, symtab, ast) = _infer_types(context, func, restype,
                                                argtypes, order=['type_infer'],
                                                **kwargs)
    return sig, symtab, ast

def infer_types_from_ast_and_sig(context, dummy_func, ast, signature, **kwargs):
    return run_pipeline(context, dummy_func, ast, signature,
                        order=['type_infer'], **kwargs)

def get_wrapper(translator, ctypes=False):
    if ctypes:
        return translator.get_ctypes_func()
    else:
        return translator.build_wrapper_function()

def compile(context, func, restype=None, argtypes=None, ctypes=False,
            compile_only=False, **kwds):
    """
    Compile a numba annotated function.

        - decompile function into a Python ast
        - run type inference using the given input types
        - compile the function to LLVM
    """
    assert 'llvm_module' not in kwds
    pipeline, (func_signature, symtab, ast) = _infer_types(
                context, func, restype, argtypes, codegen=True, **kwds)
    t = pipeline.translator

    if compile_only:
        return func_signature, t, None

    t.link()
    return func_signature, t, get_wrapper(t, ctypes)

def compile_from_sig(context, func, signature, **kwds):
    return compile(context, func, signature.return_type, signature.args,
                   **kwds)

class PipelineStage(object):
    def preconditions(self, ast, env):
        return True

    def postconditions(self, ast, env):
        return True

    def transform(self, ast, env):
        raise NotImplementedError('%r does not implement transform!' %
                                  type(self))

    def make_specializer(self, cls, ast, env, **kws):
        return cls(env.context, env.crnt.func, ast,
                   func_signature=env.crnt.func_signature,
                   nopython=env.crnt.nopython,
                   symtab=env.crnt.symtab,
                   func_name=env.crnt.func_name,
                   llvm_module=env.crnt.llvm_module,
                   **kws)

    def __call__(self, ast, env):
        assert self.preconditions(ast, env)
        ast = self.transform(ast, env)
        assert self.postconditions(ast, env)
        return ast

class ConstFolding(PipelineStage):
    def preconditions(self, ast, env):
        return not hasattr(env.crnt, 'constvars')

    def postconditions(self, ast, env):
        return hasattr(env.crnt, 'constvars')

    def transform(self, ast, env):
        const_marker = self.make_specializer(constant_folding.ConstantMarker,
                                             ast, env)
        const_marker.visit(ast)
        constvars = const_marker.get_constants()
        env.crnt.constvars = constvars
        const_folder = self.make_specializer(constant_folding.ConstantFolder,
                                             ast, env, constvars=constvars)
        return const_folder.visit(ast)

class TypeInfer(PipelineStage):
    def preconditions(self, ast, env):
        return env.crnt.symtab is not None

    def transform(self, ast, env):
        type_inferer = self.make_specializer(type_inference.TypeInferer,
                                             ast, env, locals=env.crnt.locals,
                                             **env.crnt.kwargs)
        type_inferer.infer_types()
        env.crnt.func_signature = type_inferer.func_signature
        logger.debug("signature for %s: %s", env.crnt.func_name,
                     env.crnt.func_signature)
        env.crnt.symtab = type_inferer.symtab
        return ast

class TypeSet(PipelineStage):
    def transform(self, ast, env):
        visitor = self.make_specializer(type_inference.TypeSettingVisitor, ast,
                                        env)
        visitor.visit(ast)
        return ast

class ClosureTypeInference(PipelineStage):
    def transform(self, ast, env):
        type_inferer = self.make_specializer(
                            numba.closure.ClosureTypeInferer, ast, env)
        return type_inferer.visit(ast)

class TransformFor(PipelineStage):
    def transform(self, ast, env):
        transform = self.make_specializer(transforms.TransformForIterable, ast,
                                          env)
        return transform.visit(ast)

class Specialize(PipelineStage):
    def transform(self, ast, env):
        return ast

class LateSpecializer(PipelineStage):
    def transform(self, ast, env):
        specializer = self.make_specializer(transforms.LateSpecializer, ast,
                                            env)
        return specializer.visit(ast)

class FixASTLocations(PipelineStage):
    def transform(self, ast, env):
        fixer = self.make_specializer(FixMissingLocations, ast, env)
        fixer.visit(ast)
        return ast

class CodeGen(PipelineStage):
    def transform(self, ast, env):
        env.crnt.translator = self.make_specializer(
            ast_translate.LLVMCodeGenerator, ast, env, **env.crnt.kwargs)
        env.crnt.translator.translate()
        return ast

class PipelineEnvironment(object):
    init_stages=[
        ConstFolding,
        TypeInfer,
        TypeSet,
        ClosureTypeInference,
        TransformFor,
        Specialize,
        LateSpecializer,
        FixASTLocations,
        CodeGen,
        ]

    def __init__(self, parent=None):
        self.parent = parent

    @classmethod
    def init_env(cls, context, **kws):
        ret_val = cls()
        ret_val.context = context
        for stage in cls.init_stages:
            setattr(ret_val, stage.__name__, stage)
        pipe = cls.init_stages[:]
        pipe.reverse()
        ret_val.pipeline = reduce(compose_stages, pipe)
        ret_val.__dict__.update(kws)
        ret_val.crnt = cls(ret_val)
        return ret_val

    def init_func(self, func, ast, func_signature, **kws):
        assert self.parent is not None
        self.func = func
        self.ast = ast
        self.func_signature = func_signature
        self.func_name = kws.get('name')
        if not self.func_name:
            if func:
                module_name = inspect.getmodule(func).__name__
                name = '.'.join([module_name, func.__name__])
            else:
                name = ast.name
            self.func_name = naming.specialized_mangle(
                name, self.func_signature.args)
        self.symtab = kws.get('symtab', {})
        self.llvm_module = kws.get('llvm_module')
        if self.llvm_module is None:
            self.llvm_module = self.parent.context.llvm_module
        self.nopython = kws.get('nopython')
        self.locals = kws.get('locals')
        self.kwargs = kws

def check_stage(stage):
    if isinstance(stage, str):
        def _stage(ast, env):
            return getattr(env, stage)(ast, env)
        name = stage
        _stage.__name__ = stage
        stage = _stage
    elif isinstance(stage, type) and issubclass(stage, PipelineStage):
        name = stage.__name__
        stage = stage()
    else:
        name = stage.__name__
    return name, stage

def compose_stages(f0, f1):
    f0_name, f0 = check_stage(f0)
    f1_name, f1 = check_stage(f1)
    def _numba_pipeline_composition(ast, env):
        return f0(f1(ast, env), env)
    name = '_o_'.join((f0_name, f1_name))
    _numba_pipeline_composition.__name__ = name
    return _numba_pipeline_composition
