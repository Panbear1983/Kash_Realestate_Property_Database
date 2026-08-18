"""Pure Phase 1-2 reasoning router for deterministic, allowlisted tools.

There is deliberately no I/O in this module: no model, database, network, subprocess, or
filesystem access. Phase 1 is the small routing/dispatch contract. Phase 2 is the sole
allowlisted capability, exact arithmetic and one-variable linear equations.

The parser is a recursive-descent parser over a tiny grammar. It never evaluates Python and
accepts only decimal numbers, one alphabetic variable, parentheses, and ``+ - * / =``.
Intermediate values are ``Fraction`` objects, so answers such as ``0.1 + 0.2`` are
deterministic and exact.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from fractions import Fraction
from typing import Optional

MATH_TOOL = "math"
ALLOWED_TOOLS = frozenset({MATH_TOOL})
SAFE_REPLY = "I can safely handle basic arithmetic or one-variable linear equations only."

MAX_EXPRESSION_CHARS = 200
MAX_TOKENS = 96
MAX_DEPTH = 16
MAX_NUMBER_CHARS = 30
MAX_VALUE_BITS = 256

_PREFIX = re.compile(
    r"^\s*(?:please\s+)?(?:calculate|compute|math|solve)\s*:?\s*",
    re.IGNORECASE,
)
_WHAT_IS = re.compile(r"^\s*what\s+is\s+(.+?)\s*\??\s*$", re.IGNORECASE)
_BARE_MATH = re.compile(r"^[\dA-Za-z+\-*/=()%^,.\s?]+$")
_LISTING_WORD = re.compile(
    r"\b(?:home|homes|listing|listings|property|properties|price|prices|bed|beds|"
    r"bedroom|bedrooms|bath|baths|zip|school|flood|garage)\b",
    re.IGNORECASE,
)
_SUSPICIOUS = re.compile(r"(?:__|[;`'\"\[\]{}\\]|(?:\b(?:eval|exec|import|open)\b))",
                         re.IGNORECASE)
_TOKEN = re.compile(r"(?:\d+(?:\.\d*)?|\.\d+|[xX]|[()+\-*/])")


@dataclass(frozen=True)
class ToolCall:
    """The complete allowlisted routing contract.

    ``tool`` is checked again by ``execute`` so a caller cannot construct a dataclass by hand
    to gain a capability that ``route`` would never produce.
    """

    tool: str
    expression: str


@dataclass(frozen=True)
class ToolAnswer:
    """A terminal deterministic answer for a routed call."""

    text: str
    tool: str
    ok: bool


@dataclass(frozen=True)
class _Linear:
    """An affine value ``coefficient*x + constant``."""

    coefficient: Fraction = Fraction(0)
    constant: Fraction = Fraction(0)

    @property
    def is_constant(self) -> bool:
        return self.coefficient == 0


class _MathRejected(ValueError):
    """Internal parse/safety rejection; its details are never returned to a user."""


def route(text: str) -> Optional[ToolCall]:
    """Return a math call for an unambiguous math candidate, otherwise ``None``.

    Explicit ``calculate``/``compute``/``math``/``solve`` requests are always terminal math
    candidates, including when their payload is malformed or unsafe. Bare expressions route
    only when the whole message uses the math alphabet. Suspicious expression-shaped input is
    also captured so it receives the safe reply rather than reaching a model.
    """
    raw = str(text or "").strip()
    if not raw:
        return None

    match = _PREFIX.match(raw)
    if match:
        return ToolCall(MATH_TOOL, raw[match.end():].strip().rstrip("?").strip())

    match = _WHAT_IS.match(raw)
    if match:
        candidate = match.group(1).strip()
        if (_BARE_MATH.fullmatch(candidate) and _has_math_signal(candidate)
                and _has_only_math_variables(candidate)
                and not _LISTING_WORD.search(candidate)):
            return ToolCall(MATH_TOOL, candidate)
        if _looks_unsafe_expression(candidate):
            return ToolCall(MATH_TOOL, candidate)
        return None

    candidate = raw.rstrip("?").strip()
    if (_BARE_MATH.fullmatch(candidate) and _has_math_signal(candidate)
            and _has_only_math_variables(candidate)
            and not _LISTING_WORD.search(candidate)):
        return ToolCall(MATH_TOOL, candidate)
    if _looks_unsafe_expression(candidate):
        return ToolCall(MATH_TOOL, candidate)
    return None


def execute(call: ToolCall) -> ToolAnswer:
    """Execute an allowlisted call. Unknown tools and every math error fail closed."""
    if not isinstance(call, ToolCall) or call.tool not in ALLOWED_TOOLS:
        return ToolAnswer(SAFE_REPLY, MATH_TOOL, False)
    try:
        text = _solve(call.expression)
    except (ArithmeticError, _MathRejected, ValueError):
        return ToolAnswer(SAFE_REPLY, MATH_TOOL, False)
    return ToolAnswer(text, MATH_TOOL, True)


def try_answer(text: str) -> Optional[ToolAnswer]:
    """Route and execute one message, or return ``None`` for the existing chat path."""
    call = route(text)
    return execute(call) if call is not None else None


def _has_math_signal(text: str) -> bool:
    return any(c.isdigit() for c in text) and any(c in "+-*/=%^" for c in text)


def _has_only_math_variables(text: str) -> bool:
    """Reject prose such as ``current 30-year rate`` from the bare-math fast path."""
    words = re.findall(r"[A-Za-z]+", text)
    return not words or (all(len(word) == 1 for word in words)
                         and len({word.lower() for word in words}) == 1)


def _looks_unsafe_expression(text: str) -> bool:
    """Conservatively catch code-like input that is shaped as a calculation."""
    starts_like_math = bool(re.match(r"^\s*(?:\d|\.\d|x\b)", text, re.IGNORECASE))
    starts_like_code = bool(re.match(
        r"^\s*(?:__\w+__|[a-z_]\w*(?:\.[a-z_]\w*)*)\s*\(", text, re.IGNORECASE))
    unsupported_math = bool(re.search(r"[+*/=]|\s-\s", text))
    return starts_like_code or (
        starts_like_math and (bool(_SUSPICIOUS.search(text)) or unsupported_math)
    )


def _solve(expression: str) -> str:
    expression = str(expression or "").strip()
    if not expression or len(expression) > MAX_EXPRESSION_CHARS:
        raise _MathRejected()
    if any(ord(char) < 32 or ord(char) == 127 for char in expression):
        raise _MathRejected()

    equals = expression.count("=")
    if equals > 1:
        raise _MathRejected()
    if equals == 1:
        variables = {letter.lower() for letter in re.findall(r"[A-Za-z]", expression)}
        if len(variables) != 1:
            raise _MathRejected()
        variable = variables.pop()
        expression = re.sub(re.escape(variable), "x", expression, flags=re.IGNORECASE)
        left_text, right_text = expression.split("=", 1)
        left = _Parser(left_text).parse()
        right = _Parser(right_text).parse()
        coefficient = _checked(left.coefficient - right.coefficient)
        constant = _checked(right.constant - left.constant)
        if coefficient == 0:
            if constant == 0:
                return f"Every value of {variable} is a solution."
            return "That equation has no solution."
        solution = _checked(constant / coefficient)
        return f"{variable} = {_format(solution)}"

    if re.search(r"[A-Za-z]", expression):
        raise _MathRejected()
    result = _Parser(expression).parse()
    if not result.is_constant:
        raise _MathRejected()
    return _format(result.constant)


class _Parser:
    """Parser for affine arithmetic: expression := term ((+|-) term)*."""

    def __init__(self, source: str):
        self.tokens = self._tokenize(source)
        self.index = 0
        self.depth = 0

    @staticmethod
    def _tokenize(source: str) -> list[str]:
        source = source.strip()
        if not source:
            raise _MathRejected()
        tokens: list[str] = []
        position = 0
        for match in _TOKEN.finditer(source):
            if source[position:match.start()].strip():
                raise _MathRejected()
            token = match.group(0)
            if token[0].isdigit() or token[0] == ".":
                if len(token) > MAX_NUMBER_CHARS:
                    raise _MathRejected()
            tokens.append(token.lower())
            position = match.end()
        if source[position:].strip() or not tokens or len(tokens) > MAX_TOKENS:
            raise _MathRejected()
        return tokens

    def parse(self) -> _Linear:
        value = self._expression()
        if self._peek() is not None:
            raise _MathRejected()
        return value

    def _peek(self) -> Optional[str]:
        return self.tokens[self.index] if self.index < len(self.tokens) else None

    def _take(self) -> str:
        token = self._peek()
        if token is None:
            raise _MathRejected()
        self.index += 1
        return token

    def _expression(self) -> _Linear:
        value = self._term()
        while self._peek() in ("+", "-"):
            op = self._take()
            other = self._term()
            if op == "+":
                value = _linear(value.coefficient + other.coefficient,
                                value.constant + other.constant)
            else:
                value = _linear(value.coefficient - other.coefficient,
                                value.constant - other.constant)
        return value

    def _term(self) -> _Linear:
        value = self._unary()
        while True:
            token = self._peek()
            if token is not None and token in ("*", "/"):
                op = self._take()
                other = self._unary()
            elif token == "x" or token == "(":
                # The only implicit multiplication forms needed for ordinary equations:
                # ``2x`` and ``2(x + 1)``. Nonlinear products are rejected below.
                op = "*"
                other = self._unary()
            else:
                break
            value = _multiply(value, other) if op == "*" else _divide(value, other)
        return value

    def _unary(self) -> _Linear:
        token = self._peek()
        if token is not None and token in ("+", "-"):
            op = self._take()
            value = self._unary()
            if op == "-":
                return _linear(-value.coefficient, -value.constant)
            return value
        return self._primary()

    def _primary(self) -> _Linear:
        token = self._take()
        if token == "x":
            return _Linear(Fraction(1), Fraction(0))
        if token == "(":
            self.depth += 1
            if self.depth > MAX_DEPTH:
                raise _MathRejected()
            value = self._expression()
            if self._take() != ")":
                raise _MathRejected()
            self.depth -= 1
            return value
        if token in (")", "*", "/"):
            raise _MathRejected()
        try:
            return _Linear(Fraction(0), _checked(Fraction(token)))
        except (ValueError, ZeroDivisionError):
            raise _MathRejected() from None


def _linear(coefficient: Fraction, constant: Fraction) -> _Linear:
    return _Linear(_checked(coefficient), _checked(constant))


def _multiply(left: _Linear, right: _Linear) -> _Linear:
    if not left.is_constant and not right.is_constant:
        raise _MathRejected()
    if left.is_constant:
        return _linear(right.coefficient * left.constant, right.constant * left.constant)
    return _linear(left.coefficient * right.constant, left.constant * right.constant)


def _divide(left: _Linear, right: _Linear) -> _Linear:
    if not right.is_constant or right.constant == 0:
        raise _MathRejected()
    return _linear(left.coefficient / right.constant, left.constant / right.constant)


def _checked(value: Fraction) -> Fraction:
    if (value.numerator.bit_length() > MAX_VALUE_BITS
            or value.denominator.bit_length() > MAX_VALUE_BITS):
        raise _MathRejected()
    return value


def _format(value: Fraction) -> str:
    if value.denominator == 1:
        return str(value.numerator)

    denominator = value.denominator
    for factor in (2, 5):
        while denominator % factor == 0:
            denominator //= factor
    if denominator == 1:
        # A terminating decimal, formatted without passing through binary float.
        sign = "-" if value < 0 else ""
        numerator = abs(value.numerator)
        denominator = value.denominator
        places = max(_factor_count(denominator, 2), _factor_count(denominator, 5))
        scaled = numerator * (10 ** places) // denominator
        digits = str(scaled).zfill(places + 1)
        return f"{sign}{digits[:-places]}.{digits[-places:]}".rstrip("0").rstrip(".")
    return f"{value.numerator}/{value.denominator}"


def _factor_count(number: int, factor: int) -> int:
    count = 0
    while number % factor == 0:
        number //= factor
        count += 1
    return count
