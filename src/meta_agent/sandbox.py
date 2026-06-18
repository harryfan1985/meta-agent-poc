"""§7.1 受限执行的资源边界(python_assert 等只读表达式后端的加固)。

网络 / 文件系统 / import / 属性访问 / 任意调用已被表达式语法层(_SafeExpressionValidator)
结构性挡掉。本模块补"资源上限":表达式字符数、AST 节点数、嵌套深度上限,并禁掉可致
内存膨胀的序列/数值重复与幂(`*` / `**` / `@`)——如 `"x" * 10**9`、`[0] * 10**8`。

纯静态、进程内、可移植:不依赖 fork/seccomp,适配 darwin;对 PoC 足够把不受信表达式的
资源耗尽面收口。真实多进程沙箱(setrlimit/seccomp)留作部署期强隔离。
"""
from __future__ import annotations

import ast

MAX_EXPR_CHARS = 2000
MAX_AST_NODES = 200
MAX_AST_DEPTH = 25

# 可致 O(n) 内存/CPU 膨胀的算子:序列/字符串重复、超大幂、矩阵乘。
_RESOURCE_RISK_NODES = (ast.Mult, ast.Pow, ast.MatMult)


class SandboxLimitError(ValueError):
    """表达式超出资源边界(长度/节点数/深度)或使用了被禁的膨胀算子。"""


def _max_depth(node: ast.AST, depth: int = 0) -> int:
    children = list(ast.iter_child_nodes(node))
    if not children:
        return depth
    return max(_max_depth(c, depth + 1) for c in children)


def enforce_expression_limits(expression: str, tree: ast.AST) -> None:
    """对已解析的表达式施加资源上限;违反则抛 SandboxLimitError。"""
    if len(expression) > MAX_EXPR_CHARS:
        raise SandboxLimitError(
            f"expression too long: {len(expression)} > {MAX_EXPR_CHARS} chars")
    nodes = list(ast.walk(tree))
    if len(nodes) > MAX_AST_NODES:
        raise SandboxLimitError(f"expression too complex: {len(nodes)} > {MAX_AST_NODES} AST nodes")
    depth = _max_depth(tree)
    if depth > MAX_AST_DEPTH:
        raise SandboxLimitError(f"expression too deeply nested: depth {depth} > {MAX_AST_DEPTH}")
    for n in nodes:
        if isinstance(n, _RESOURCE_RISK_NODES):
            raise SandboxLimitError(
                f"operator {type(n).__name__} disabled to bound memory/CPU "
                "(sequence/number repetition can exhaust resources)")
