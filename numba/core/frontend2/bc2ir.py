from dataclasses import dataclass, field, replace
import dis
from typing import Optional

from . import bcinterp

from numba_rvsdg.core.datastructures.byte_flow import ByteFlow
from numba_rvsdg.core.datastructures.scfg import SCFG
from numba_rvsdg.core.datastructures.basic_block import (
    BasicBlock,
    PythonBytecodeBlock,
    RegionBlock,
)
from numba_rvsdg.rendering.rendering import ByteFlowRenderer

from .renderer import RvsdgRenderer



@dataclass(frozen=True)
class ValueState:
    parent: Optional["Op"]
    name: str
    out_index: int
    is_effect: bool = False

    def short_identity(self) -> str:
        return f"ValueState({id(self.parent):x}, {self.name}, {self.out_index})"


@dataclass(frozen=True)
class Op:
    opname: str
    bc_inst: Optional[dis.Instruction]
    _inputs: dict[str, ValueState] = field(default_factory=dict)
    _outputs: dict[str, ValueState] = field(default_factory=dict)

    def add_input(self, name, vs: ValueState):
        self._inputs[name] = vs

    def add_output(self, name: str, is_effect=False) -> ValueState:
        vs = ValueState(parent=self, name=name, out_index=len(self._outputs), is_effect=is_effect)
        self._outputs[name] = vs
        return vs

    def short_identity(self) -> str:
        return f"Op({self.opname}, {id(self):x})"


@dataclass(frozen=True)
class DDGBlock(BasicBlock):
    in_effect: ValueState | None = None
    out_effect: ValueState | None = None
    in_stackvars: tuple[ValueState, ...] = ()
    out_stackvars: tuple[ValueState, ...] = ()
    out_vars: dict[str, ValueState] = field(default_factory=dict)

    def render_rvsdg(self, renderer, digraph, label, block):
        with digraph.subgraph(name="cluster_"+str(label)) as g:
            g.attr(color='lightgrey')
            g.attr(label=str(label))
            # render body
            self.render_valuestate(renderer, g, self.in_effect)
            self.render_valuestate(renderer, g, self.out_effect)
            for vs in self.in_stackvars:
                self.render_valuestate(renderer, g, vs)
            for vs in self.out_stackvars:
                self.render_valuestate(renderer, g, vs)
            for vs in self.out_vars.values():
                self.render_valuestate(renderer, g, vs)
            g.node(str(label), shape="doublecircle", label="")

    def render_valuestate(self, renderer, digraph, vs: ValueState, *, follow=True):
        if vs.is_effect:
            digraph.node(vs.short_identity(), shape="circle", label=str(vs.name))
        else:
            digraph.node(vs.short_identity(), shape="rect", label=str(vs.name))
        if follow and vs.parent is not None:
            op = vs.parent
            self.render_op(renderer, digraph, op)

    def render_op(self, renderer, digraph, op: Op):
        op_anchor = op.short_identity()
        digraph.node(op_anchor, label=op_anchor,
                     shape="box", style="rounded")
        for vs in op._outputs.values():
            self.add_vs_edge(renderer, op_anchor, vs)
            self.render_valuestate(renderer, digraph, vs, follow=False)
        for vs in op._inputs.values():
            self.add_vs_edge(renderer, vs, op_anchor)
            self.render_valuestate(renderer, digraph, vs)

    def add_vs_edge(self, renderer, src, dst):
        is_effect = (isinstance(src, ValueState) and src.is_effect) or (isinstance(dst, ValueState) and dst.is_effect)
        if isinstance(src, ValueState):
            src = src.short_identity()
        if isinstance(dst, ValueState):
            dst = dst.short_identity()
        if is_effect:
            kwargs = dict(style="dotted")
        else:
            kwargs = {}
        renderer.add_edge(src, dst, **kwargs)



def render_scfg(byteflow):
    bfr = ByteFlowRenderer()
    bfr.bcmap_from_bytecode(byteflow.bc)
    bfr.render_scfg(byteflow.scfg).view("scfg")


def build_rvsdg(code):
    byteflow = ByteFlow.from_bytecode(code)
    byteflow = byteflow.restructure()
    rvsdg = convert_to_dataflow(byteflow)
    RvsdgRenderer().render_scfg(rvsdg).view("rvsdg")


def convert_to_dataflow(byteflow: ByteFlow) -> SCFG:
    bcmap = {inst.offset: inst for inst in byteflow.bc}
    return convert_scfg_to_dataflow(byteflow.scfg, bcmap)


def convert_scfg_to_dataflow(scfg, bcmap) -> SCFG:
    rvsdg = SCFG()
    for label, block in scfg.graph.items():
        # convert block
        if isinstance(block, PythonBytecodeBlock):
            ddg = convert_bc_to_ddg(block, bcmap)
            rvsdg.add_block(ddg)
        elif isinstance(block, RegionBlock):
            subregion = convert_scfg_to_dataflow(block.subregion, bcmap)
            rvsdg.add_block(replace(block, subregion=subregion))
        else:
            rvsdg.add_block(block)
    return rvsdg


def convert_bc_to_ddg(block: PythonBytecodeBlock, bcmap: dict[int, dis.Bytecode]):
    instlist = block.get_instructions(bcmap)
    converter = BC2DDG()
    in_effect = converter.effect
    for inst in instlist:
        converter.convert(inst)
    blk = DDGBlock(
        label=block.label,
        _jump_targets=block._jump_targets,
        backedges=block.backedges,
        in_effect=in_effect,
        out_effect=converter.effect,
        in_stackvars=tuple(converter.incoming_stackvars),
        out_stackvars=tuple(converter.stack),
        out_vars=converter.varmap,
    )

    return blk

class BC2DDG:
    def __init__(self):
        self.stack: list[ValueState] = []
        start_env = Op("start", bc_inst=None)
        self.effect = start_env.add_output("env", is_effect=True)
        self.varmap: dict[str, ValueState] = {}
        self.incoming_vars: dict[str, ValueState] = {}
        self.incoming_stackvars: list[ValueState] = []

    def push(self, val: ValueState):
        self.stack.append(val)

    def pop(self) -> ValueState:
        if not self.stack:
            op = Op(opname="stack.incoming", bc_inst=None)
            vs = op.add_output(f"stack[{len(self.incoming_stackvars)}]")
            self.stack.append(vs)
            self.incoming_stackvars.append(vs)
        return self.stack.pop()

    def store(self, varname: str, value: ValueState):
        self.varmap[varname] = value

    def load(self, varname: str) -> ValueState:
        if varname not in self.varmap:
            op = Op(opname="var.incoming", bc_inst=None)
            vs = op.add_output(varname)
            self.incoming_vars[varname] = vs
            self.varmap[varname] = vs

        return self.varmap[varname]

    def replace_effect(self, env: ValueState):
        assert env.is_effect
        self.effect = env

    def convert(self, inst: dis.Instruction):
        fn = getattr(self, f"op_{inst.opname}")
        fn(inst)

    def op_RESUME(self, inst: dis.Instruction):
        pass   # no-op

    def op_LOAD_GLOBAL(self, inst: dis.Instruction):
        op = Op(opname="global", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_output("value")
        self.push(op.add_output("out"))

    def op_LOAD_CONST(self, inst: dis.Instruction):
        op = Op(opname="const", bc_inst=inst)
        self.push(op.add_output("out"))

    def op_STORE_FAST(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="store", bc_inst=inst)
        op.add_input("value", tos)
        self.store(inst.argval, op.add_output(inst.argval))

    def op_LOAD_FAST(self, inst: dis.Instruction):
        self.push(self.load(inst.argval))

    def op_PRECALL(self, inst: dis.Instruction):
        pass # no-op

    def op_CALL(self, inst: dis.Instruction):
        argc: int = inst.argval
        callable = self.pop()  # TODO
        arg0 = self.pop() # TODO
        # TODO: handle kwnames
        args = reversed([arg0, *[self.pop() for _ in range(argc)]])
        op = Op(opname="call", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("callee", callable)
        for i, arg in enumerate(args):
            op.add_input(f"arg.{i}", arg)
        self.replace_effect(op.add_output("env", is_effect=True))
        self.push(op.add_output("ret"))

    def op_GET_ITER(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="getiter", bc_inst=inst)
        op.add_input("obj", tos)
        self.push(op.add_output("iter"))

    def op_FOR_ITER(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="foriter", bc_inst=inst)
        op.add_input("iter", tos)
        self.push(op.add_output("indvar"))

    def op_BINARY_OP(self, inst: dis.Instruction):
        rhs = self.pop()
        lhs = self.pop()
        op = Op(opname="binaryop", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("lhs", lhs)
        op.add_input("rhs", rhs)
        self.replace_effect(op.add_output("env", is_effect=True))
        self.push(op.add_output("out"))

    def op_RETURN_VALUE(self, inst: dis.Instruction):
        tos = self.pop()
        op = Op(opname="ret", bc_inst=inst)
        op.add_input("env", self.effect)
        op.add_input("retval", tos)
        self.replace_effect(op.add_output("env", is_effect=True))

    def op_JUMP_BACKWARD(self, inst: dis.Instruction):
        pass # no-op


def run_frontend(func): #, inline_closures=False, emit_dels=False):
    # func_id = bytecode.FunctionIdentity.from_function(func)

    rvsdg = build_rvsdg(func.__code__)

    return rvsdg
    # bc = bytecode.ByteCode(func_id=func_id)
    # interp = bcinterp.Interpreter(func_id)
    # func_ir = interp.interpret(bc)
    # return func_ir
