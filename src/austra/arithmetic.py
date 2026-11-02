"""Bounded decimal expression evaluation without executing model-authored code."""

import ast
import re
from decimal import Decimal, InvalidOperation, localcontext

MAX_NODES = 80
MAX_DEPTH = 16
MAX_DIGITS = 60
MAX_OUTPUT_CHARS = 200
NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")


def _number(value: str) -> Decimal:
    if len(value) > MAX_DIGITS:
        raise ValueError("Decimal input is too long")
    try:
        number = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("Invalid decimal input") from error
    if not number.is_finite():
        raise ValueError("Decimal input must be finite")
    if abs(number.adjusted()) > 100:
        raise ValueError("Decimal input is outside the supported range")
    return number


def validate_expression(expression: str, variable_names: set[str]) -> ast.Expression:
    if len(expression) > 500 or len(variable_names) > 20:
        raise ValueError("Calculation is too large")
    if any(not NAME.fullmatch(name) for name in variable_names):
        raise ValueError("Invalid variable name")
    try:
        root = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("Invalid calculation expression") from error
    if sum(1 for _ in ast.walk(root)) > MAX_NODES:
        raise ValueError("Calculation has too many operations")

    def check(node: ast.AST, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ValueError("Calculation is too deeply nested")
        if isinstance(node, ast.Expression):
            check(node.body, depth + 1)
        elif isinstance(node, ast.Name):
            if node.id not in variable_names:
                raise ValueError(f"Missing input {node.id}")
        elif isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            _number(ast.get_source_segment(expression, node) or str(node.value))
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            check(node.operand, depth + 1)
        elif isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            check(node.left, depth + 1)
            check(node.right, depth + 1)
        else:
            raise ValueError("Only decimal arithmetic and named inputs are allowed")

    check(root, 0)
    return root


def calculate_expression(expression: str, variables: dict[str, str]) -> str:
    root = validate_expression(expression, set(variables))
    values = {name: _number(value) for name, value in variables.items()}

    def evaluate(node: ast.AST, depth: int) -> Decimal:
        if depth > MAX_DEPTH:
            raise ValueError("Calculation is too deeply nested")
        if isinstance(node, ast.Expression):
            return evaluate(node.body, depth + 1)
        if isinstance(node, ast.Name):
            if node.id not in values:
                raise ValueError(f"Missing input {node.id}")
            return values[node.id]
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            source = ast.get_source_segment(expression, node)
            return _number(source or str(node.value))
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            value = evaluate(node.operand, depth + 1)
            return value if isinstance(node.op, ast.UAdd) else -value
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left = evaluate(node.left, depth + 1)
            right = evaluate(node.right, depth + 1)
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if right == 0:
                raise ValueError("Division by zero")
            return left / right
        raise ValueError("Only decimal arithmetic and named inputs are allowed")

    with localcontext() as context:
        context.prec = 40
        try:
            result = evaluate(root, 0)
        except InvalidOperation as error:
            raise ValueError("Calculation is outside the supported range") from error
    output = format(result, "f")
    if len(output) > MAX_OUTPUT_CHARS:
        raise ValueError("Calculation result is too large")
    return output
