import math
import operator
from collections import defaultdict, namedtuple
from dataclasses import dataclass
from functools import partial
from typing import Any, Dict, List, Optional, Tuple

import sympy

import torch
import torch.fx

from torch._export.graph_module import get_export_meta
from torch._export.pass_base import ExportPassBase, ProxyValue
from torch._export.pass_infra.node_metadata import NodeMetadata
from torch.fx.passes.infra.pass_base import PassResult


__all__ = ["AddRuntimeAssertionsForConstraintsPass"]


@dataclass
class ConstraintSpec:
    constraint_dim: int

@dataclass
class RangeConstraintSpec(ConstraintSpec):
    min_val: int
    max_val: int

@dataclass
class EqualityConstraintSpec(ConstraintSpec):
    other_name: str
    other_dim: int

# Convert simple sympy Integers into concrete int to
# insert into graph
def _convert_to_int(val):
    if val == sympy.oo:
        return math.inf
    if isinstance(val, sympy.Integer):
        return int(val)
    raise RuntimeError(
        "Export constraints cannot be non-integer expressions"
    )

class AddRuntimeAssertionsForConstraintsPass(ExportPassBase):
    def __init__(self) -> None:
        super().__init__()
        self.current_gm: Optional[torch.fx.GraphModule] = None

    def _process_shape_constraints(self, constraints) -> Dict[str, List[ConstraintSpec]]:
        input_name_to_dim_constraints = defaultdict(
            lambda: {"range": defaultdict(list), "equality": defaultdict(list)}
        )
        for name, shape_constraints in constraints.items():
            for dim, min_val, max_val in shape_constraints["range"]:
                input_name_to_dim_constraints[name]["range"][dim].append(
                    (_convert_to_int(min_val), _convert_to_int(max_val))
                )
            for dim, other_name, other_dim in shape_constraints["equality"]:
                input_name_to_dim_constraints[name]["equality"][dim].append(
                    (other_name, other_dim)
                )

        # Merge the constraints into a single list of constraints
        input_name_to_constraints = defaultdict(list)
        for name, dim_constraints in input_name_to_dim_constraints.items():
            for dim, range_constraints in dim_constraints["range"].items():
                if range_constraints:
                    min_vals, max_vals = zip(*range_constraints)
                    min_val = sorted(min_vals, reverse=True)[0]
                    max_val = sorted(max_vals, reverse=False)[0]
                    assert min_val <= max_val
                    input_name_to_constraints[name].append(
                        RangeConstraintSpec(
                            constraint_dim=dim,
                            min_val=min_val,
                            max_val=max_val,
                        )
                    )
            for dim, eq_constraints in dim_constraints["equality"].items():
                for other_name, other_dim in eq_constraints:
                    input_name_to_constraints[name].append(
                        EqualityConstraintSpec(
                            constraint_dim=dim,
                            other_name=other_name,
                            other_dim=other_dim,
                        )
                    )

        return input_name_to_constraints

    def call(self, graph_module: torch.fx.GraphModule) -> PassResult:
        self.current_gm = graph_module
        assert isinstance(self.current_gm, torch.fx.GraphModule)
        self.constraints = self._process_shape_constraints(get_export_meta(self.current_gm).input_shape_constraints)
        self.input_name_to_example_inputs = get_export_meta(self.current_gm).input_name_to_example_inputs
        self.inline_constraints = get_export_meta(self.current_gm).inline_constraints
        self.input_name_to_args = {}
        return super().call(graph_module)

    def _insert_specialized_shapes_assert(self, arg, dims, name, current_inp):
        # we don't want to get shapes from meta as they will be symbolic
        shapes = current_inp.shape
        for dim in dims:
            assert_msg = (
                f"Input {name}'s dimension #{dim} size is "
                f"specialized at {shapes[dim]}"
            )
            dim_node = super().call_operator(
                torch.ops.aten.sym_size,
                (arg, dim),
                {},
                NodeMetadata({}),
            )
            eq = super().call_operator(
                operator.eq,
                (dim_node, shapes[dim]),
                {},
                NodeMetadata({}),
            )
            tensor_eq = super().call_operator(
                torch.ops.aten.scalar_tensor.default,
                (eq,),
                {},
                NodeMetadata({}),
            )
            super().call_operator(
                torch.ops.aten._assert_async.msg,
                (tensor_eq, assert_msg),
                {},
                NodeMetadata({}),
            )

    def placeholder(self, name: str, arg, meta) -> ProxyValue:
        arg = super().placeholder(name, arg, meta)
        if name not in self.input_name_to_example_inputs:
            return arg
        self.input_name_to_args[name] = arg
        return arg

    def postprocess_placeholders(self):
        assert self.current_gm is not None
        equality_constraints = []
        for name, arg in self.input_name_to_args.items():
            current_inp = self.input_name_to_example_inputs[name]
            all_dims = set(range(current_inp.dim()))

            # If no dynamism is specified, we assume all dimensions are specialized
            if name not in self.constraints:
                self._insert_specialized_shapes_assert(arg, all_dims, name, current_inp)
                return arg

            constraints = self.constraints[name]

            constrained_dims = set()
            # Add runtime asserts for user-specified range constraints for each
            # individual dimension. Equality constraints like x[1] == x[0] are
            # handled later.
            for constraint in constraints:
                constrained_dims.add(constraint.constraint_dim)
                dim = super().call_operator(
                    torch.ops.aten.sym_size,
                    (arg, constraint.constraint_dim),
                    {},
                    NodeMetadata({}),
                )
                if isinstance(constraint, RangeConstraintSpec):
                    assert_msg = (
                        f"Input {name}'s dimension #{constraint.constraint_dim} size is "
                        f"outside of specified dynamic range [{constraint.min_val}, {constraint.max_val}]"
                    )
                    # TODO (tmanlaibaatar) we are making an assumption that graph generated for
                    # input dim N >=2 generalizes to N < 2. Ideally we should check that:
                    # 1. if we can generalize to N < 2, not add any assertion saying N >= 2
                    # 2. If we can't generalize to N < 2, add an assertion saying N >= 2
                    # Above can be achieved via a seperate pass.
                    self._assert_range_constraint(dim, constraint.min_val, constraint.max_val, assert_msg, low_threshold=2)
                else:
                    assert isinstance(constraint, EqualityConstraintSpec)
                    equality_constraints.append((constraint, name, dim))

            specialized_dims = all_dims - constrained_dims
            # Make all non-constrained dims to be static
            self._insert_specialized_shapes_assert(arg, specialized_dims, name, current_inp)

        for constraint, name, dim in equality_constraints:
            # Add runtime asserts for user-specified equality constraints.
            other_arg = self.input_name_to_args[constraint.other_name]
            other_dim = super().call_operator(
                torch.ops.aten.sym_size,
                (other_arg, constraint.other_dim),
                {},
                NodeMetadata({}),
            )
            assert_msg = (
                f"Input {name}'s dimension #{constraint.constraint_dim} size is "
                f"not equal to input {constraint.other_name}'s dimension #{constraint.other_dim}"
            )
            self._assert_equality_constraint(dim, other_dim, assert_msg)

    def _assert_range_constraint(self, proxy, lower, upper, assert_msg, low_threshold=2):
        if lower > low_threshold:
            self._insert_assert_async(operator.ge, proxy, lower, assert_msg)

        if upper < math.inf:
            self._insert_assert_async(operator.le, proxy, upper, assert_msg)

    def _assert_equality_constraint(self, proxy1, proxy2, assert_msg):
        self._insert_assert_async(operator.eq, proxy1, proxy2, assert_msg)

    def _insert_assert_async(self, operator, l, r, assert_msg):
        cmp = super().call_operator(operator, (l, r), {}, NodeMetadata({}))
        cmp_tensor = super().call_operator(torch.ops.aten.scalar_tensor.default, (cmp,), {}, NodeMetadata({}))
        super().call_operator(
            torch.ops.aten._assert_async.msg,
            (cmp_tensor, assert_msg),
            {},
            NodeMetadata({}),
        )

    def call_operator(self, op, args, kwargs, meta) -> ProxyValue:
        ret = super().call_operator(op, args, kwargs, meta)
        if "val" in meta:
            val = meta["val"]

            # In general, we may have to deal the case such as: ret[1].shape[0].
            # We need first find out what symbols require assertion, then we need to follow the path
            # from ret to the symbol, construct the proxies along the way and construct the messages
            # piece-wise at the same time.
            #
            # We use post-order traversal to collect all the proxies callbacks needed, construct
            # the error message callbacks, and at the top-level traversal tree we execute all the callbacks.
            # We need the callbacks because, in order to call the function to create a proxy for shape[0], we
            # need the proxy for shape, which further requries the proxy for ret[1], etc.
            def add_assertions(val):
                call_backs = []
                messages = []
                if isinstance(val, (torch.SymInt, torch.SymFloat, torch.SymBool)):
                    expr = val.node._expr
                    if expr in self.inline_constraints:
                        constraint = self.inline_constraints[expr]
                        lower = _convert_to_int(constraint.lower)
                        upper = _convert_to_int(constraint.upper)
                        assert_msg = f" is outside of inline constraint [{lower}, {upper}]."
                        call_backs.append(partial(self._assert_constraint, lower=lower, upper=upper, low_threshold=-1))
                        messages.append(assert_msg)
                elif isinstance(val, torch.Tensor):
                    for i, sym in enumerate(val.shape):
                        cbs, msgs = add_assertions(sym)
                        for cb, msg in zip(cbs, msgs):
                            def sym_size_cb(proxy, assert_msg, dim):
                                dim_proxy = super(AddRuntimeAssertionsForConstraintsPass, self).call_operator(
                                    torch.ops.aten.sym_size,
                                    (proxy, dim),
                                    {},
                                    NodeMetadata({}),
                                )
                                cb(proxy=dim_proxy, assert_msg=assert_msg)
                            call_backs.append(partial(sym_size_cb, dim=i))
                            messages.append(f".shape[{i}]" + msg)
                return call_backs, messages
            callbacks, messages = add_assertions(val)
            for cb, msg in zip(callbacks, messages):
                cb(proxy=ret, assert_msg=f"{ret.node}" + msg)
        return ret
