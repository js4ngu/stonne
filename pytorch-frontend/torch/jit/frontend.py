import torch
import sys
import ast
import inspect
import string
from textwrap import dedent
from torch._C._jit_tree_views import (
    ClassDef, Ident, Stmt, Decl, Def, Var,
    EmptyTypeAnnotation, Param, ExprStmt, Assign,
    Delete, Return, Raise, Assert, AugAssign, While,
    For, If, Pass, Break, Continue, Apply, Dots, Select,
    TrueLiteral, FalseLiteral, NoneLiteral, Starred,
    ListLiteral, TupleLiteral, DictLiteral, Const,
    StringLiteral, ListComp, Attribute, BinOp, UnaryOp,
    SliceExpr, Subscript, TernaryIf, With, WithItem, Property,
)
from torch._utils_internal import get_source_lines_and_file

from torch._jit_internal import SourceContext, should_drop, is_static_fn
import torch.jit.annotations

# Borrowed from cPython implementation
# https://github.com/python/cpython/blob/561612d8456cfab5672c9b445521113b847bd6b3/Lib/textwrap.py#L411#

_reserved_prefix = '__jit'
_reserved_names = {'print'}
_identifier_chars = set(string.ascii_lowercase + string.ascii_uppercase + string.digits)


def is_reserved_name(name):
    return name.startswith(_reserved_prefix) or name in _reserved_names


pretty_node_names = {
    ast.FunctionDef: "function definitions",
    ast.For: "for loops",
    ast.Delete: "del statements",
    ast.ClassDef: "class definitions",
    ast.With: "with statements",
    ast.Raise: "raise statements",
    ast.Assert: "assertions",
    ast.Import: "import statements",
    ast.ImportFrom: "import statements",
    ast.Global: "global variables",
    ast.Break: "break statements",
    ast.Continue: "continue statements",
}

node_start_tokens = {
    ast.FunctionDef: "def",
    ast.For: "for",
    ast.Delete: "del",
    ast.ClassDef: "class",
    ast.With: "with",
    ast.Raise: "raise",
    ast.Assert: "assert",
    ast.Import: "import",
    ast.ImportFrom: "from",
    ast.Global: "global",
    ast.Break: "break",
    ast.Continue: "continue",
}

pretty_node_names.update({
    ast.AsyncFunctionDef: "async function definitions",
    ast.AsyncFor: "async for loops",
    ast.AsyncWith: "async with statements",
    ast.Try: "try blocks",
    ast.Nonlocal: "nonlocal variables",
})

node_start_tokens.update({
    ast.AsyncFunctionDef: "async def",
    ast.AsyncFor: "async for",
    ast.AsyncWith: "async with",
    ast.Try: "try",
    ast.Nonlocal: "nonlocal",
})

if sys.version_info >= (3, 6):
    pretty_node_names.update({
        ast.AnnAssign: "annotated assignments",
    })
    # NB: no specific token for AnnAssign


class FrontendError(Exception):
    def __init__(self, source_range, msg):
        self.source_range = source_range
        self.msg = msg

        # This has to be instantiated here so the ErrorReport is accurate to the
        # call stack when the FrontendError was raised
        self.error_report = torch._C.ErrorReport(self.source_range)

    def __str__(self):
        return self.msg + self.error_report.what().lstrip()


class NotSupportedError(FrontendError):
    pass


class UnsupportedNodeError(NotSupportedError):
    def __init__(self, ctx, offending_node, reason=''):
        # If we don't have a specific token, we default to length of 1
        node_type = type(offending_node)
        range_len = len(node_start_tokens.get(node_type, ' '))
        source_range = ctx.make_range(offending_node.lineno,
                                      offending_node.col_offset,
                                      offending_node.col_offset + range_len)
        feature_name = pretty_node_names.get(node_type, node_type.__name__)
        msg = "{} {}aren't supported".format(feature_name, reason + ' ' if reason else '')
        super(UnsupportedNodeError, self).__init__(source_range, msg)


class FrontendTypeError(FrontendError):
    pass


def build_withitems(ctx, items):
    items = [build_withitem(ctx, i) for i in items]
    return list(items)


def build_stmts(ctx, stmts):
    stmts = [build_stmt(ctx, s) for s in stmts]
    return list(filter(None, stmts))


def get_class_properties(cls, self_name):
    """
    Get a list of Property objects representing the properties of a class.

    Arguments:
        cls:  The class to get properties of.
        self_name: The name of the class that the properties should belong to.
    Returns:
        A list of Property objects corresponding to the properties of cls. Property
        here refers to the subclass of TreeView.
    """
    props = inspect.getmembers(
        cls, predicate=lambda m: isinstance(m, property))

    # Create Property TreeView objects from inspected property objects.
    properties = []
    for prop in props:
        getter = get_jit_def(prop[1].fget, f"__{prop[0]}_getter", self_name=self_name)
        setter = get_jit_def(prop[1].fset, f"__{prop[0]}_setter", self_name=self_name) if prop[1].fset else None
        properties.append(Property(getter.range(), Ident(getter.range(), prop[0]), getter, setter))

    return properties


def get_jit_class_def(cls, self_name):
    # Get defs for each method within the current class independently
    # TODO: proper overriding analysis when implementing class inheritance
    methods = inspect.getmembers(
        cls,
        predicate=lambda m: (inspect.ismethod(m) or inspect.isfunction(m))
        and not is_static_fn(cls, m.__name__)
        and m.__name__ in cls.__dict__
    )
    methods = [get_jit_def(method[1],
                           method[0],
                           self_name=self_name) for method in methods]

    properties = get_class_properties(cls, self_name)

    sourcelines, file_lineno, filename = get_source_lines_and_file(cls, torch._C.ErrorReport.call_stack())
    source = ''.join(sourcelines)
    dedent_src = dedent(source)
    py_ast = ast.parse(dedent_src)
    leading_whitespace_len = len(source.split('\n', 1)[0]) - len(dedent_src.split('\n', 1)[0])
    ctx = SourceContext(source, filename, file_lineno, leading_whitespace_len, False)
    return build_class_def(ctx, py_ast.body[0], methods, properties, self_name)


def get_jit_def(fn, def_name, self_name=None):
    """
    Build a JIT AST (TreeView) from the given function.

    Arguments:
        fn: A function object to compile
        def_name: The name to give to the resulting AST object. This is not
            always the same as `fn.__name__`, for example:
                def _forward(self):
                    ...
                forward = _forward
            In this case, the `__name__` attribute of the function object is "_forward",
            but we want the result AST to have the name "forward".
        self_name: If this function is a method, what the type name of `self` is.
    """
    sourcelines, file_lineno, filename = get_source_lines_and_file(fn, torch._C.ErrorReport.call_stack())
    source = ''.join(sourcelines)
    dedent_src = dedent(source)
    py_ast = ast.parse(dedent_src)
    if len(py_ast.body) != 1 or not isinstance(py_ast.body[0], ast.FunctionDef):
        raise RuntimeError("Expected a single top-level function")
    leading_whitespace_len = len(source.split('\n', 1)[0]) - len(dedent_src.split('\n', 1)[0])
    type_line = torch.jit.annotations.get_type_line(source)
    ctx = SourceContext(source, filename, file_lineno, leading_whitespace_len, True)
    fn_def = py_ast.body[0]

    # Swap out the function signature and body if it is unused
    if should_drop(fn):
        unused_fn_def = ast.parse("def unused_fn(self: Any):\n\traise RuntimeError(\"Cannot call @unused methods\")").body[0]
        fn_def.body = unused_fn_def.body
        # kwarg/vararg not supported by `build_def`
        fn_def.args.kwarg = fn_def.args.vararg = None
        for arg in fn_def.args.args + fn_def.args.kwonlyargs:
            # Replace potentially unsupported type annotations by "Any"
            arg.annotation = unused_fn_def.args.args[0].annotation

    return build_def(ctx, fn_def, type_line, def_name, self_name=self_name)


class Builder(object):
    def __call__(self, ctx, node):
        method = getattr(self, 'build_' + node.__class__.__name__, None)
        if method is None:
            raise UnsupportedNodeError(ctx, node)
        return method(ctx, node)


def build_class_def(ctx, py_def, methods, properties, self_name):
    r = ctx.make_range(py_def.lineno, py_def.col_offset,
                       py_def.col_offset + len("class"))
    return ClassDef(Ident(r, self_name), [Stmt(method) for method in methods], properties)


def build_def(ctx, py_def, type_line, def_name, self_name=None):
    body = py_def.body
    r = ctx.make_range(py_def.lineno + len(py_def.decorator_list),
                       py_def.col_offset,
                       py_def.col_offset + len("def"))
    param_list = build_param_list(ctx, py_def.args, self_name)
    return_type = None
    if getattr(py_def, 'returns', None) is not None:
        return_type = build_expr(ctx, py_def.returns)
    decl = Decl(r, param_list, return_type)
    is_method = self_name is not None
    if type_line is not None:
        type_comment_decl = torch._C.parse_type_comment(type_line)
        decl = torch._C.merge_type_from_type_comment(decl, type_comment_decl, is_method)

    return Def(Ident(r, def_name),
               decl,
               build_stmts(ctx, body))


_vararg_kwarg_err = ("Compiled functions can't take variable number of arguments "
                     "or use keyword-only arguments with defaults")


def build_param_list(ctx, py_args, self_name):
    if py_args.kwarg is not None:
        expr = py_args.kwarg
        ctx_range = ctx.make_range(expr.lineno, expr.col_offset - 1, expr.col_offset + len(expr.arg))
        raise NotSupportedError(ctx_range, _vararg_kwarg_err)
    if py_args.vararg is not None:
        expr = py_args.vararg
        ctx_range = ctx.make_range(expr.lineno, expr.col_offset - 1, expr.col_offset + len(expr.arg))
        raise NotSupportedError(ctx_range, _vararg_kwarg_err)
    if len(py_args.kw_defaults) > 0:
        # kw_defaults is a list of the values for the kwargs (which default to None),
        # so they don't actually have line numbers.
        for arg in py_args.kw_defaults:
            if arg is not None:
                ctx_range = build_expr(ctx, arg).range()
                raise NotSupportedError(ctx_range, _vararg_kwarg_err)
    result = [build_param(ctx, arg, self_name, False) for arg in py_args.args]
    result += [build_param(ctx, arg, self_name, True) for arg in py_args.kwonlyargs]
    return result


def build_param(ctx, py_arg, self_name, kwarg_only):
    # NB: In Python3 py_arg is a pair of (str arg, expr? annotation)
    name = py_arg.arg
    r = ctx.make_range(py_arg.lineno, py_arg.col_offset, py_arg.col_offset + len(name))
    if getattr(py_arg, 'annotation', None) is not None:
        annotation_expr = build_expr(ctx, py_arg.annotation)
    elif self_name is not None and name == 'self':
        annotation_expr = Var(Ident(r, self_name))
    else:
        annotation_expr = EmptyTypeAnnotation(r)
    return Param(annotation_expr, Ident(r, name), kwarg_only)


def get_default_args(fn):
    if fn is None:
        return {}

    signature = inspect.signature(fn)
    return {
        k: v.default
        for k, v in signature.parameters.items()
        if v.default is not inspect.Parameter.empty
    }


class WithItemBuilder(Builder):
    @staticmethod
    def build_withitem(ctx, item):
        lineno = item.context_expr.lineno
        start = item.context_expr.col_offset
        end = start + len(pretty_node_names[ast.With])
        op_vars = item.optional_vars
        r = ctx.make_range(lineno, start, end)

        return WithItem(r, build_expr(ctx, item.context_expr), build_expr(ctx, op_vars) if op_vars else None)


class StmtBuilder(Builder):
    augassign_map = {
        ast.Add: '+',
        ast.Sub: '-',
        ast.Mult: '*',
        ast.Div: '/',
        ast.Mod: '%',
    }

    @staticmethod
    def build_Expr(ctx, stmt):
        value = stmt.value
        if value.__class__.__name__ == 'Str':
            # If a statement is a string literal expression,
            # then it is a docstring. Just ignore it.
            return None
        else:
            return ExprStmt(build_expr(ctx, value))

    @staticmethod
    def build_Assign(ctx, stmt):
        rhs = build_expr(ctx, stmt.value)
        lhs = list(map(lambda x: build_expr(ctx, x), stmt.targets))
        return Assign(lhs, rhs)

    @staticmethod
    def build_AnnAssign(ctx, stmt):
        if stmt.value is None:
            raise UnsupportedNodeError(ctx, stmt, reason='without assigned value')
        rhs = build_expr(ctx, stmt.value)
        lhs = build_expr(ctx, stmt.target)
        the_type = build_expr(ctx, stmt.annotation)
        return Assign([lhs], rhs, the_type)

    @staticmethod
    def build_Delete(ctx, stmt):
        if len(stmt.targets) > 1:
            source_range = ctx.make_range(stmt.lineno, stmt.col_offset,
                                          stmt.col_offset + len("del"))
            raise NotSupportedError(
                source_range, 'del with more than one operand is not supported')
        return Delete(build_expr(ctx, stmt.targets[0]))

    @staticmethod
    def build_Return(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("return"))
        return Return(r, None if stmt.value is None else build_expr(ctx, stmt.value))

    @staticmethod
    def build_Raise(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("raise"))
        expr = build_expr(ctx, stmt.exc)
        return Raise(r, expr)

    @staticmethod
    def build_Assert(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("assert"))
        test = build_expr(ctx, stmt.test)
        msg = build_expr(ctx, stmt.msg) if stmt.msg is not None else None
        return Assert(r, test, msg)

    @staticmethod
    def build_AugAssign(ctx, stmt):
        lhs = build_expr(ctx, stmt.target)
        rhs = build_expr(ctx, stmt.value)
        op = type(stmt.op)
        if op in StmtBuilder.augassign_map:
            op_token = StmtBuilder.augassign_map[op]
        else:
            raise NotSupportedError(
                find_before(ctx, rhs.range().start, '=', offsets=(-1, 0)),
                "unsupported kind of augumented assignment: " + op.__name__)
        return AugAssign(lhs, op_token, rhs)

    @staticmethod
    def build_While(ctx, stmt):
        if stmt.orelse:
            # TODO: try to recover the location of else:? Python doesn't give us useful
            # annotations in this case
            raise NotSupportedError(None, "else branches of while loops aren't supported")
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("while"))
        return While(r, build_expr(ctx, stmt.test),
                     build_stmts(ctx, stmt.body))

    @staticmethod
    def build_For(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("for"))
        return For(
            r, [build_expr(ctx, stmt.target)],
            [build_expr(ctx, stmt.iter)], build_stmts(ctx, stmt.body))

    @staticmethod
    def build_If(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("if"))
        return If(r, build_expr(ctx, stmt.test),
                  build_stmts(ctx, stmt.body),
                  build_stmts(ctx, stmt.orelse))

    @staticmethod
    def build_Print(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("print"))
        if stmt.dest:
            raise NotSupportedError(r, "print statements with non-default destinations aren't supported")
        args = [build_expr(ctx, val) for val in stmt.values]
        return ExprStmt(Apply(Var(Ident(r, "print")), args, []))

    @staticmethod
    def build_Pass(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("pass"))
        return Pass(r)

    @staticmethod
    def build_Break(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("break"))
        return Break(r)

    @staticmethod
    def build_Continue(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("continue"))
        return Continue(r)

    @staticmethod
    def build_With(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset + len("with"))
        return With(r, build_withitems(ctx, stmt.items), build_stmts(ctx, stmt.body))

class ExprBuilder(Builder):
    binop_map = {
        ast.Add: '+',
        ast.Sub: '-',
        ast.Mult: '*',
        ast.Div: '/',
        ast.Pow: '**',
        ast.Mod: '%',
        ast.FloorDiv: '//',
        ast.BitAnd: '&',
        ast.BitXor: '^',
        ast.BitOr: '|',
        ast.LShift: '<<',
        ast.RShift: '>>',
    }

    binop_map[ast.MatMult] = '@'

    unop_map = {
        ast.Not: 'not',
        ast.USub: '-',
        ast.Invert: '~',
    }

    boolop_map = {
        ast.And: 'and',
        ast.Or: 'or',
    }

    cmpop_map = {
        ast.Eq: '==',
        ast.NotEq: '!=',
        ast.LtE: '<=',
        ast.Lt: '<',
        ast.GtE: '>=',
        ast.Gt: '>',
        ast.Is: 'is',
        ast.IsNot: 'is not',
        ast.In: 'in',
        ast.NotIn: 'not in',
    }

    @staticmethod
    def build_Attribute(ctx, expr):
        base = build_expr(ctx, expr.value)
        # expr.attr is just a string, so it's not annotated in any way, so we have
        # to build the range manually
        source = ctx.source.encode('utf-8')

        def get_char(index):
            return chr(source[index])

        start_pos = base.range().end + 1
        while get_char(start_pos) in string.whitespace:  # Skip whitespace
            start_pos += 1
        end_pos = start_pos + len(expr.attr)
        name_range = ctx.make_raw_range(start_pos, end_pos)
        return Select(base, Ident(name_range, expr.attr))

    @staticmethod
    def build_Call(ctx, expr):
        func = build_expr(ctx, expr.func)
        args = [build_expr(ctx, py_arg) for py_arg in expr.args]
        if hasattr(expr, 'starargs') and expr.starargs:
            stararg_expr = build_expr(ctx, expr.starargs)
            args += [Starred(stararg_expr.range(), stararg_expr)]
        kwargs = []
        for kw in expr.keywords:
            kw_expr = build_expr(ctx, kw.value)
            # XXX: we could do a better job at figuring out the range for the name here
            if not kw.arg:
                raise NotSupportedError(kw_expr.range(), 'keyword-arg expansion is not supported')
            kwargs.append(Attribute(Ident(kw_expr.range(), kw.arg), kw_expr))
        return Apply(func, args, kwargs)

    @staticmethod
    def build_Ellipsis(ctx, expr):
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 3)  # len("...") == 3
        return Dots(r)

    @staticmethod
    def build_Name(ctx, expr):
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + len(expr.id))
        if expr.id.startswith(_reserved_prefix):
            raise NotSupportedError(r, "names of variables used in JIT-ed functions "
                                       "can't start with " + _reserved_prefix)
        if expr.id == "True":
            return TrueLiteral(r)
        elif expr.id == "False":
            return FalseLiteral(r)
        elif expr.id == "None":
            return NoneLiteral(r)
        return Var(Ident(r, expr.id))

    @staticmethod
    def build_NameConstant(ctx, expr):
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + len(str(expr.value)))
        if expr.value is True:
            return TrueLiteral(r)
        elif expr.value is False:
            return FalseLiteral(r)
        elif expr.value is None:
            return NoneLiteral(r)
        else:
            raise ValueError("Name constant value unsupported: " + str(expr.value))

    @staticmethod
    def build_BinOp(ctx, expr):
        lhs = build_expr(ctx, expr.left)
        rhs = build_expr(ctx, expr.right)
        op = type(expr.op)

        if op == ast.Div and not ctx.uses_true_division:
            err_range = ctx.make_raw_range(lhs.range().end, rhs.range().start)
            raise FrontendError(err_range, 'Division of ints in TorchScript uses Python 3 true '
                                'division semantics. Please put `from __future__ '
                                'import division` at the top of your file')
        op_token = ExprBuilder.binop_map.get(op)
        if op_token is None:
            err_range = ctx.make_raw_range(lhs.range().end, rhs.range().start)
            raise NotSupportedError(err_range, "unsupported binary operator: " + op.__name__)
        return BinOp(op_token, lhs, rhs)

    @staticmethod
    def build_UnaryOp(ctx, expr):
        sub_expr = build_expr(ctx, expr.operand)
        op = type(expr.op)
        op_token = ExprBuilder.unop_map.get(op)
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + len(op_token))
        if op_token is None:
            err_range = ctx.make_raw_range(r.start, sub_expr.range().end)
            raise NotSupportedError(err_range, "unsupported unary operator: " + op.__name__)
        return UnaryOp(r, op_token, sub_expr)

    @staticmethod
    def build_BoolOp(ctx, expr):
        if len(expr.values) < 2:
            raise AssertionError("expected at least 2 values in BoolOp, but got " + str(len(expr.values)))
        sub_exprs = [build_expr(ctx, sub_expr) for sub_expr in expr.values]
        op = type(expr.op)
        op_token = ExprBuilder.boolop_map.get(op)
        if op_token is None:
            err_range = ctx.make_raw_range(sub_exprs[0].range().end, sub_exprs[1].range().start)
            raise NotSupportedError(err_range, "unsupported boolean operator: " + op.__name__)
        lhs = sub_exprs[0]
        for rhs in sub_exprs[1:]:
            lhs = BinOp(op_token, lhs, rhs)
        return lhs

    @staticmethod
    def build_IfExp(ctx, expr):
        return TernaryIf(build_expr(ctx, expr.test),
                         build_expr(ctx, expr.body),
                         build_expr(ctx, expr.orelse))

    @staticmethod
    def build_Compare(ctx, expr):
        operands = [build_expr(ctx, e) for e in [expr.left] + list(expr.comparators)]
        result = None
        for lhs, op_, rhs in zip(operands, expr.ops, operands[1:]):
            op = type(op_)
            op_token = ExprBuilder.cmpop_map.get(op)
            r = ctx.make_raw_range(lhs.range().end, rhs.range().start)
            if op_token is None:
                raise NotSupportedError(r, "unsupported comparison operator: " + op.__name__)

            if op == ast.NotIn:
                # NB: `not in` is just `not( in )`, so we don't introduce new tree view
                # but just make it a nested call in our tree view structure
                in_expr = BinOp('in', lhs, rhs)
                cmp_expr = UnaryOp(r, 'not', in_expr)
            else:
                cmp_expr = BinOp(op_token, lhs, rhs)

            if result is None:
                result = cmp_expr
            else:
                result = BinOp('and', result, cmp_expr)
        return result

    @staticmethod
    def build_Subscript(ctx, expr):
        def build_SliceExpr(ctx, base, slice_expr):
            lower = build_expr(ctx, slice_expr.lower) if slice_expr.lower is not None else None
            upper = build_expr(ctx, slice_expr.upper) if slice_expr.upper is not None else None
            step = build_expr(ctx, slice_expr.step) if slice_expr.step is not None else None
            return SliceExpr(base.range(), lower, upper, step)

        def build_Index(ctx, base, index_expr):
            if isinstance(index_expr.value, ast.Tuple) or \
                    isinstance(index_expr.value, ast.List):
                raise NotSupportedError(base.range(),
                                        "slicing multiple dimensions with "
                                        "sequences not supported yet")
            return build_expr(ctx, index_expr.value)

        def build_ExtSlice(ctx, base, extslice):
            sub_exprs = []
            for expr in extslice.dims:
                sub_type = type(expr)
                if sub_type is ast.Index:
                    sub_exprs.append(build_Index(ctx, base, expr))
                elif sub_type is ast.Slice:
                    sub_exprs.append(build_SliceExpr(ctx, base, expr))
                elif sub_type is ast.Ellipsis:
                    sub_exprs.append(Dots(base.range()))
                else:
                    raise NotSupportedError(base.range(),
                                            "slicing multiple dimensions with "
                                            "{} not supported".format(sub_type))
            return sub_exprs

        base = build_expr(ctx, expr.value)
        sub_type = type(expr.slice)
        if sub_type is ast.Index:
            if isinstance(expr.slice.value, ast.Tuple):
                # N-dimensional indexing using Tuple: x[(i, j, k)] is equivalent to x[i, j, k]
                # XXX: Indexing using a list is **different**! It triggers advanced indexing.
                indices = []
                for index_expr in expr.slice.value.elts:
                    indices.append(build_expr(ctx, index_expr))
                return Subscript(base, indices)
            else:
                return Subscript(base, [build_expr(ctx, expr.slice.value)])
        elif sub_type is ast.Slice:
            return Subscript(base, [build_SliceExpr(ctx, base, expr.slice)])
        elif sub_type is ast.ExtSlice:
            return Subscript(base, build_ExtSlice(ctx, base, expr.slice))
        else:  # Ellipsis (can only happen in Python 2)
            raise NotSupportedError(base.range(), "ellipsis is not supported")

    @staticmethod
    def build_List(ctx, expr):
        return ListLiteral(ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1),
                           [build_expr(ctx, e) for e in expr.elts])

    @staticmethod
    def build_Tuple(ctx, expr):
        return TupleLiteral(ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1),
                            [build_expr(ctx, e) for e in expr.elts])

    @staticmethod
    def build_Dict(ctx, expr):
        return DictLiteral(ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1),
                           [build_expr(ctx, e) for e in expr.keys], [build_expr(ctx, e) for e in expr.values])

    @staticmethod
    def build_Num(ctx, expr):
        value = str(expr.n)
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + len(value))
        return Const(r, value)

    @staticmethod
    def build_Constant(ctx, expr):
        value = expr.value
        if value is None or isinstance(value, bool):
            # NB: this check has to happen before the int check because bool is
            # a subclass of int
            return ExprBuilder.build_NameConstant(ctx, expr)
        if isinstance(value, (int, float)):
            return ExprBuilder.build_Num(ctx, expr)
        elif isinstance(value, str):
            return ExprBuilder.build_Str(ctx, expr)
        elif isinstance(value, type(Ellipsis)):
            return ExprBuilder.build_Ellipsis(ctx, expr)
        else:
            error_range = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + len(str(value)))
            raise FrontendError(error_range, "Unknown Constant expression type")

    @staticmethod
    def build_Str(ctx, expr):
        value = str(expr.s)
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1)
        return StringLiteral(r, value)

    @staticmethod
    def build_JoinedStr(ctx, expr):
        s = ''
        args = []
        for value in expr.values:
            r = ctx.make_range(value.lineno, value.col_offset, value.col_offset + 1)
            if isinstance(value, ast.FormattedValue):
                if value.conversion != -1:
                    raise NotSupportedError(r, 'Don\'t support conversion in JoinedStr')
                if value.format_spec is not None:
                    raise NotSupportedError(r, 'Don\'t support formatting in JoinedStr')
                s += '{}'
                args.append(build_expr(ctx, value.value))
            elif isinstance(value, ast.Str):
                s += value.s
            else:
                raise NotSupportedError(r, 'Unsupported value in JoinedStr')

        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1)
        return Apply(Select(StringLiteral(r, s), Ident(r, 'format')), args, [])

    @staticmethod
    def build_ListComp(ctx, stmt):
        r = ctx.make_range(stmt.lineno, stmt.col_offset, stmt.col_offset)
        if (len(stmt.generators) > 1):
            raise NotSupportedError(r, "multiple comprehension generators not supported yet")

        if (len(stmt.generators[0].ifs) != 0):
            raise NotSupportedError(r, "comprehension ifs not supported yet")

        elt_expr = build_expr(ctx, stmt.elt)
        target_expr = build_expr(ctx, stmt.generators[0].target)

        iter_expr = build_expr(ctx, stmt.generators[0].iter)
        return ListComp(r, elt_expr, target_expr, iter_expr)

    @staticmethod
    def build_Starred(ctx, expr):
        r = ctx.make_range(expr.lineno, expr.col_offset, expr.col_offset + 1)
        return Starred(r, build_expr(ctx, expr.value))

build_expr = ExprBuilder()
build_stmt = StmtBuilder()
build_withitem = WithItemBuilder()

def find_before(ctx, pos, substr, offsets=(0, 0)):
    new_pos = ctx.source[:pos].rindex(substr)
    return ctx.make_raw_range(new_pos + offsets[0], new_pos + len(substr) + offsets[1])