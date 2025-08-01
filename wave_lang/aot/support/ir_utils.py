# Copyright 2023 Nod Labs, Inc
# Portions Copyright 2022 The IREE Authors
#
# Licensed under the Apache License v2.0 with LLVM Exceptions.
# See https://llvm.org/LICENSE.txt for license information.
# SPDX-License-Identifier: Apache-2.0 WITH LLVM-exception

import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from itertools import zip_longest
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

import numpy as np
import torch

from iree.compiler.extras.fx_importer import (
    ContextCache,
    RefTracker,
)
from wave_lang.dynamo.type_conversion import (
    NativeTypeConverter,
)
from wave_lang.support.conversions import (
    TORCH_DTYPE_TO_IREE_TYPE,
)
from wave_lang.support.ir_imports import (
    ArrayAttr,
    Attribute,
    BF16Type,
    Context,
    DenseElementsAttr,
    DenseResourceElementsAttr,
    DictAttr,
    F16Type,
    F32Type,
    F64Type,
    Float4E2M1FNType,
    Float6E2M3FNType,
    Float8E4M3FNType,
    Float8E4M3FNUZType,
    Float8E5M2FNUZType,
    Float8E5M2Type,
    Float8E8M0FNUType,
    FloatAttr,
    FunctionType,
    IndexType,
    InsertionPoint,
    IntegerAttr,
    IntegerType,
    VectorType,
    IrType,
    Location,
    MLIRError,
    Operation,
    RankedTensorType,
    StringAttr,
    SymbolTable,
    TypeAttr,
    UnitAttr,
    Value,
    arith_d,
    func_d,
    tensor_d,
    vector_d,
)
from wave_lang.support.logging import aot_logger as logger

from ..tensor_traits import (
    DeviceAffinity,
    DeviceTensorTrait,
    ExternalTensorTrait,
)

###############################################################################
# Configuration
###############################################################################

# Maps a name to an altered name. If returns None, then the original
# name is used (this lets dict.get serve as a NameMapCallback).
NameMapCallback = Callable[[str], Optional[str]]


class GlobalAttributes:
    """Settings for how to initialize the global."""

    __slots__ = [
        "external",
        "external_scope",
        "mutable",
        "name_mapper",
        "noinline",
        "uninitialized",
    ]

    def __init__(
        self,
        mutable: bool = False,
        external: Optional[bool] = None,
        external_scope: Optional[str] = None,
        name_mapper: Optional[NameMapCallback] = None,
        noinline: bool = False,
        uninitialized: Optional[bool] = None,
    ):
        if external and uninitialized:
            raise ValueError(
                "Globals with external=True cannot also have uninitialized=True",
            )
        if uninitialized and not mutable:
            raise ValueError(
                "Globals with uninitialized=True must also be mutable=True",
            )
        self.mutable = mutable
        self.external = external
        self.external_scope = external_scope
        self.name_mapper = name_mapper
        self.noinline = noinline
        self.uninitialized = uninitialized

    def map_name(self, name: str) -> str:
        if self.name_mapper:
            new_name = self.name_mapper(name)
            if new_name is not None:
                return new_name
        return name

    def infer_external_from_tensor(
        self,
        t: torch.Tensor,
    ) -> Tuple[bool, Optional[str], Optional[str]]:
        """If externality is not specified, infers it from the tensor."""
        # We check for the first item in a list because this lets us in the
        # future extend the list by unwrapping.
        check_tensors = [t]
        for check_t in check_tensors:
            trait = ExternalTensorTrait.get(check_t)
            if trait is None:
                continue
            try:
                external_scope = trait.external_scope
                external_name = trait.external_name
            except AttributeError:
                raise AttributeError(
                    f"Tensor defines _is_turbine_external_tensor but not other fields: {type(t)} = {t}",
                )
            return (
                True,
                external_scope if self.external_scope is None else self.external_scope,
                external_name,
            )

        return bool(self.external), self.external_scope, None


###############################################################################
# Builders
###############################################################################


@dataclass
class ModuleBuilderOptions:
    # Whether to import torch symbolic shape expressions for ExportedPrograms.
    import_symbolic_shape_expressions: bool = False


class ModuleBuilder:
    """Wrapper around module and IR accounting for a module being built."""

    __slots__ = [
        "_auto_symbol_counts",
        "body",
        "cache",
        "context",
        "fx_py_attr_tracker",
        "global_ref_tracker",
        "ip",
        "last_global_op",
        "module_op",
        "native_type_converter",
        "options",
        "symbol_table",
    ]

    def __init__(
        self,
        module_op: Operation,
        *,
        options: Optional[ModuleBuilderOptions] = None,
    ):
        self.module_op = module_op
        self.options = options or ModuleBuilderOptions()
        self.context = module_op.context
        self.body = module_op.regions[0].blocks[0]
        self.symbol_table = SymbolTable(module_op)
        # We organize globals in order of declaration at the top of the module.
        # To do so, record the last one emitted so that newly created ones
        # can be ordered properly.
        self.last_global_op: Optional[Operation] = None
        self.ip = InsertionPoint(self.body)
        self.cache = ContextCache(self.context)
        # Tracks global references to a MaterializedGlobal.
        self.global_ref_tracker = RefTracker()
        # Usually the FxImporter makes a new ref tracker for each invocation,
        # but we want to preserve it across individual JIT evaluations so
        # as to better intern tensors to attributes.
        self.fx_py_attr_tracker = RefTracker()
        self.native_type_converter = NativeTypeConverter(self.context)
        self._auto_symbol_counts: Dict[str, int] = {}

    def unique_auto_symbol(self, requested_name: str) -> str:
        if requested_name not in self._auto_symbol_counts:
            self._auto_symbol_counts[requested_name] = 0
            return requested_name
        count = self._auto_symbol_counts[requested_name] + 1
        self._auto_symbol_counts[requested_name] = count
        return f"{requested_name}${count}"

    def handle_mlir_error(self, op: Operation, e: MLIRError, message: str):
        # TODO: Replace with a real dumping facility.
        # See: https://github.com/nod-ai/SHARK-ModelDev/issues/136
        dump_path = Path(tempfile.gettempdir()) / "turbine_module_builder_error.mlir"
        logger.exception(f"{message} (dumping to {dump_path})")
        try:
            with open(dump_path, "wb") as f:
                op.print(
                    file=f,
                    binary=True,
                    print_generic_op_form=True,
                    large_elements_limit=100,
                )
            logger.debug(f"Dump complete to {dump_path}")
        except Exception:
            logger.exception("Error generating dump file")

    def finalize_construct(self):
        try:
            self.module_op.verify()
        except MLIRError as e:
            self.handle_mlir_error(self.module_op, e, "module failed to verify")
            raise

    def create_func_op(
        self,
        symbol_name: str,
        argument_types: Sequence[IrType],
        is_public: bool = True,
        add_entry_block: bool = True,
        # Array of DictAttr corresponding to the attributes for each argument.
        argument_attributes: ArrayAttr | list[DictAttr] | None = None,
    ) -> Tuple[str, func_d.FuncOp]:
        with self.ip:
            ftype = FunctionType.get(argument_types, [])
            func_op = func_d.FuncOp(symbol_name, ftype)
            if not is_public:
                func_op.attributes["sym_visibility"] = StringAttr.get("private")
            if add_entry_block:
                func_op.add_entry_block()
            self.symbol_table.insert(func_op)
            actual_symbol_name = StringAttr(func_op.attributes["sym_name"]).value
            if argument_attributes is not None:
                func_op.arg_attrs = argument_attributes
            return actual_symbol_name, func_op

    def torch_dtype_to_iree_type(self, dtype: torch.dtype) -> IrType:
        try:
            with self.context:
                return TORCH_DTYPE_TO_IREE_TYPE[dtype]()
        except KeyError:
            raise TypeError(f"Could not map Torch dtype {dtype} to an IREE type")

    def create_tensor_global(
        self,
        symbol_name: str,
        t: torch.Tensor,
        *,
        attrs: GlobalAttributes,
        logical_name: Optional[str] = None,
    ) -> Tuple[str, Operation, IrType]:
        element_type = self.torch_dtype_to_iree_type(t.dtype)
        external, external_scope, external_name = attrs.infer_external_from_tensor(t)
        device = DeviceTensorTrait.get(t)

        # Always create globals at the top. Then after created, if there was
        # a prior one, move the new one to after it to maintain declaration
        # order.
        with InsertionPoint.at_block_begin(self.body), Location.unknown():
            tensor_type = RankedTensorType.get(list(t.shape), element_type)
            ir_attrs = {
                "sym_name": StringAttr.get(symbol_name),
                "sym_visibility": StringAttr.get("private"),
                "type": TypeAttr.get(tensor_type),
            }
            if attrs.noinline:
                ir_attrs["noinline"] = UnitAttr.get()
            if attrs.mutable:
                ir_attrs["is_mutable"] = UnitAttr.get()
            if device:
                if device.queues is None:
                    ir_attrs["stream.affinity"] = Attribute.parse(
                        f"#hal.device.promise<@__device_{device.ordinal}>",
                    )
                else:
                    queues = ", ".join(device.queues)
                    ir_attrs["stream.affinity"] = Attribute.parse(
                        f"#hal.device.promise<@__device_{device.ordinal}, [{queues}]>",
                    )

            if external:
                # Emit named external reference.
                external_scope_attr = StringAttr.get(external_scope or "model")
                external_name = (
                    external_name
                    if external_name is not None
                    else attrs.map_name(
                        logical_name if logical_name is not None else symbol_name,
                    )
                )
                external_name_attr = StringAttr.get(external_name)
                # TODO: Have real Python builders for this.
                ir_attrs["initial_value"] = Attribute.parse(
                    f"#stream.parameter.named<{external_scope_attr}::{external_name_attr}> : {tensor_type}",
                )
            elif attrs.uninitialized:
                # Emit unitialized initial_value to signal that the memory
                # is valid but has undefined contents.
                # TODO: Have real Python builders for this.
                ir_attrs["initial_value"] = Attribute.parse(
                    f"#util.uninitialized : {tensor_type}",
                )
            else:
                # Emit inline initialized.
                detached_tensor = t.detach().contiguous().cpu()
                array = np.array(detached_tensor)
                # We know that a Numpy array is a ReadableBuffer so ignore type error.
                contents = memoryview(array)  # type: ignore
                blob_name = symbol_name
                elements_attr = DenseResourceElementsAttr.get_from_buffer(
                    contents,
                    blob_name,
                    tensor_type,
                )
                ir_attrs["initial_value"] = elements_attr

            global_op = Operation.create("util.global", attributes=ir_attrs)
            self.symbol_table.insert(global_op)
            if self.last_global_op is not None:
                global_op.move_after(self.last_global_op)
            self.last_global_op = global_op
            actual_symbol_name = StringAttr(global_op.attributes["sym_name"]).value
            return actual_symbol_name, global_op, tensor_type

    def create_typed_global(
        self,
        symbol_name: str,
        global_type: IrType,
        *,
        attrs: GlobalAttributes,
        logical_name: Optional[str] = None,
    ) -> Tuple[str, Operation]:
        # Always create globals at the top. Then after created, if there was
        # a prior one, move the new one to after it to maintain declaration
        # order.
        with InsertionPoint.at_block_begin(self.body), Location.unknown():
            ir_attrs = {
                "sym_name": StringAttr.get(symbol_name),
                "sym_visibility": StringAttr.get("private"),
                "type": TypeAttr.get(global_type),
            }
            if attrs.noinline:
                ir_attrs["noinline"] = UnitAttr.get()
            if attrs.mutable:
                ir_attrs["is_mutable"] = UnitAttr.get()
            if attrs.uninitialized:
                # Emit unitialized initial_value to signal that the memory
                # is valid but has undefined contents.
                # TODO: Have real Python builders for this.
                ir_attrs["initial_value"] = Attribute.parse(
                    f"#util.uninitialized : {global_type}",
                )
            else:
                # Initialized by default.
                ir_attrs["initial_value"] = self._create_initial_value_for_type(
                    global_type,
                )
            global_op = Operation.create("util.global", attributes=ir_attrs)
            self.symbol_table.insert(global_op)
            if self.last_global_op is not None:
                global_op.move_after(self.last_global_op)
            self.last_global_op = global_op
            actual_symbol_name = StringAttr(global_op.attributes["sym_name"]).value
            return actual_symbol_name, global_op

    def _create_initial_value_for_type(self, t: IrType) -> Attribute:
        # TODO(#169): Implement something upstream for this (it exists in the C++ API)
        # and use it.
        if RankedTensorType.isinstance(t):
            rtt = RankedTensorType(t)
            if not rtt.has_static_shape:
                raise ValueError(
                    "Cannot create initialization value for dynamic shaped tensor",
                )
            element_attr = self._create_initial_value_for_type(rtt.element_type)
            return DenseElementsAttr.get_splat(t, element_attr)
        if IntegerType.isinstance(t):
            return IntegerAttr.get(t, 0)
        if F32Type.isinstance(t) or F64Type.isinstance(t) or F16Type.isinstance(t):
            # TODO(#170): There should be a common way to check if a FloatType.
            return FloatAttr.get(t, 0.0)
        if IndexType.isinstance(t):
            return IntegerAttr.get(IndexType.get(), 0)
        raise ValueError(
            f"Cannot create a default initialization value for type {t}",
        )


class FunctionBuilder:
    """Helpers for building function bodies."""

    __slots__ = [
        "context",
        "func_op",
        "ip",
        "loc",
        "module_builder",
        "return_types",
    ]

    def __init__(
        self,
        *,
        module_builder: ModuleBuilder,
        func_op: func_d.FuncOp,
    ):
        self.module_builder = module_builder
        self.func_op = func_op
        self.context = func_op.context
        self.ip = InsertionPoint(self.func_op.entry_block)
        self.return_types: Optional[Sequence[IrType]] = None
        self.loc = self.func_op.location

    def emit_return(self, *ir_values: Value):
        with self.loc, self.ip:
            func_d.ReturnOp(ir_values)
            # Check or rewrite the function return type.
            value_types = [v.type for v in ir_values]
            if self.return_types:
                if value_types != self.return_types:
                    raise ValueError(
                        f"Multi-return function must return same types. "
                        f"{value_types} vs {self.return_types}",
                    )
                return
            self.return_types = value_types
            ftype = self.func_op.type
            ftype = FunctionType.get(ftype.inputs, value_types)
            self.func_op.attributes["function_type"] = TypeAttr.get(ftype)
            try:
                self.func_op.verify()
            except MLIRError as e:
                self.module_builder.handle_mlir_error(
                    self.func_op,
                    e,
                    "created function does not verify",
                )
                raise


###############################################################################
# Helpers
###############################################################################


def build_index_attribute(value: int) -> IntegerAttr:
    return IntegerAttr.get(IndexType.get(), value)


def build_index_value(
    value: int,
    constant_cache: Optional[dict[int, Value]] = None,
) -> Value:
    if constant_cache is not None and value in constant_cache:
        return constant_cache[value]
    index_value = arith_d.ConstantOp(IndexType.get(), value).result
    if constant_cache is not None:
        constant_cache[value] = index_value
    return index_value


def build_tensor_dim_value(
    t: Value,
    dim: int,
    constant_cache: Optional[dict[int, Value]] = None,
) -> Value:
    dim_value = build_index_value(dim, constant_cache=constant_cache)
    return tensor_d.DimOp(t, dim_value).result


# API name  inspired by mlir/python/mlir/dialects/_arith_ops_ext.py
def _is_float_type(type):
    return isinstance(
        type,
        (
            BF16Type,
            F16Type,
            F32Type,
            F64Type,
            Float8E4M3FNType,
            Float8E4M3FNUZType,
            Float8E5M2Type,
            Float8E5M2FNUZType,
            Float8E8M0FNUType,
            Float8E8M0FNUType,
            Float6E2M3FNType,
            Float4E2M1FNType,
        ),
    )


def _is_index_type(type):
    return isinstance(type, (IndexType))


def _is_integer_like_type(type):
    return isinstance(type, (IntegerType, IndexType))


def _is_signed_or_signless_type(type):
    return getattr(type, "is_signed", False) or getattr(type, "is_signless", False)


def get_conversion_op(src_elem_type, dst_elem_type, fastmath=None):
    is_src_float = _is_float_type(src_elem_type)
    is_dst_float = _is_float_type(dst_elem_type)
    is_src_int = _is_integer_like_type(src_elem_type)
    is_dst_int = _is_integer_like_type(dst_elem_type)
    if (
        is_src_int
        and is_dst_int
        and (_is_index_type(src_elem_type) or _is_index_type(dst_elem_type))
    ):
        conversion_op = arith_d.index_cast
        return conversion_op
    # Special case of casting bool (IntergerType(i1) to float) so that when value is true is casted to 1 and when value is false to cast to 0
    if (
        isinstance(src_elem_type, IntegerType)
        and src_elem_type.width == 1
        and is_dst_float
    ):

        def bool_to_float_select(dst_type, vector_src):

            # scalar constants
            one_const = arith_d.constant(
                dst_elem_type, FloatAttr.get(dst_elem_type, 1.0)
            )
            zero_const = arith_d.constant(
                dst_elem_type, FloatAttr.get(dst_elem_type, 0.0)
            )

            # Broadcast to vector if the destination is a vector
            if VectorType.isinstance(dst_type):
                one = vector_d.broadcast(dst_type, one_const)
                zero = vector_d.broadcast(dst_type, zero_const)
            elif RankedTensorType.isinstance(dst_type):
                raise NotImplementedError(
                    "RankedTensorType broadcasting is not implemented for casting bool to float."
                )
            else:
                one = one_const
                zero = zero_const

            return arith_d.select(vector_src, one, zero)

        # return caller function for get_conversion op
        return bool_to_float_select

    conversion_ops = {
        (True, False): arith_d.fptosi,
        (False, True): arith_d.sitofp,
    }

    float_cast_ops = {
        True: arith_d.extf,
        False: arith_d.truncf,
    }

    int_cast_ops = {
        True: arith_d.extsi,
        False: arith_d.trunci,
    }

    if is_src_float and is_dst_float:
        conversion_op = float_cast_ops[src_elem_type.width < dst_elem_type.width]
        conversion_op = partial(conversion_op, fastmath=fastmath)
    elif is_src_int and is_dst_int:
        # Currently extsi/trunci do not support fast_math option.
        conversion_op = int_cast_ops[src_elem_type.width < dst_elem_type.width]
    else:
        conversion_op = conversion_ops[(is_src_float, is_dst_float)]
    return conversion_op


def _attribute_from_device_affinity(
    affinity: DeviceAffinity,
    context: Context,
) -> Attribute:
    return Attribute.parse(
        f'#hal.device.promise<@"__device_{affinity.ordinal}">',
        context,
    )


def attributes_from_argument_device_affinities(
    affinities: dict[int, DeviceAffinity] | None,
    arguments_count: int,
    context: Context,
) -> list[dict[str, Attribute]]:
    """Get as attributes for function op arguments."""
    if affinities is None:
        return [{} for _ in range(arguments_count)]
    return [
        (
            {
                "iree.abi.affinity": _attribute_from_device_affinity(
                    affinities[i],
                    context,
                ),
            }
            if i in affinities
            else {}
        )
        for i in range(arguments_count)
    ]


def update_func_op_argument_attributes(
    func_op: func_d.FuncOp,
    attributes: list[dict[str, Attribute]],
):
    if func_d.ARGUMENT_ATTRIBUTE_NAME not in func_op.attributes:
        mutable_arg_attrs: list[dict[str, Attribute]] = [
            {} for _ in range(len(func_op.arguments))
        ]
    else:
        mutable_arg_attrs = [
            {named_attr.name: named_attr.attr for named_attr in dict_attr}
            for dict_attr in func_op.arg_attrs
        ]

    for src, dst in zip_longest(attributes, mutable_arg_attrs):
        dst.update(src)

    func_op.arg_attrs = [
        DictAttr.get(d, context=func_op.context) for d in mutable_arg_attrs
    ]
