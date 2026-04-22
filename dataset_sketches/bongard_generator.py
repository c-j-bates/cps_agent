"""
Bongard Text Problem Generator (v2)

Generates Bongard-style problems where:
  - 6 positive items and 6 negative items are presented
  - A ground-truth classifier (from the DSL) separates them
  - Negatives are "close" to positives (targeted perturbation)
  - No simpler classifier also separates all examples (ambiguity check)
  - Trivial classifiers are filtered by base rate

Usage:
    python -m dataset_sketches.bongard_generator --n 50 --output dataset.csv
"""

import csv
import json
import math
import random
import string
import argparse
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable

from dataset_sketches.bongard_dsl import (
    FEATURE_REGISTRY,
    CATEGORY_DIFFICULTY,
    Classifier,
    # All features
    length, count_alpha, count_digits, count_symbols,
    count_uppercase, count_lowercase, count_vowels, count_consonants,
    count_unique, max_run_length, num_runs, count_char_type_transitions,
    first_char_type, last_char_type,
    is_palindrome, all_chars_same, all_chars_unique, first_equals_last,
    starts_with_vowel, ends_with_vowel, starts_with_digit, ends_with_digit,
    has_digits, has_letters, has_symbols, has_repeated_char,
    is_alternating_case, is_alternating_char_type,
    alpha_sum, alpha_product, letters_sorted_ascending, letters_sorted_descending,
    letters_contiguous, alpha_diffs_constant, alpha_diffs_positive, alpha_step_size,
    digit_sum, digit_product, num_even_digits, num_odd_digits,
    max_digit, min_digit, digit_range,
    digits_sorted_ascending, digits_sorted_descending,
    digit_diffs_constant, digit_diffs_increasing,
    digits_all_same_parity, digits_alternating_parity, opposite_parity_digits,
    all_digits_even, all_digits_odd,
    all_letters_vowels, all_letters_consonants,
    numeric_value, is_prime_number, is_perfect_square, is_power_of_2, is_fibonacci,
    num_divisors, is_palindrome_number, binary_digit_count, binary_length,
    ascii_sum, scrabble_score, morse_length, roman_numeral_length,
    count_enclosed_regions, count_curved_letters, count_ascenders, count_descenders,
    count_symmetric_chars,
    cv_pattern_is_palindrome, is_periodic, shortest_period, has_repeated_bigram,
    vowel_ratio, uppercase_ratio, unique_ratio,
    # Predicates
    eq, neq, gt, lt, gte, lte, between, is_even, is_odd, _is_prime, divisible_by, modulo_eq,
    # Combinators
    AND, OR, NOT,
    # Constants
    VOWELS, CONSONANTS, SYMMETRIC_CHARS, ENCLOSED_REGIONS,
)


# ============================================================================
# ITEM SPACES
# ============================================================================

class ItemSpace:
    """Defines a space of string items and how to sample/perturb them."""

    def __init__(self, name: str, charset: str, min_len: int, max_len: int,
                 description: str = ""):
        self.name = name
        self.charset = charset
        self.min_len = min_len
        self.max_len = max_len
        self.description = description

    def sample(self) -> str:
        """Sample a random item from this space."""
        n = random.randint(self.min_len, self.max_len)
        return "".join(random.choice(self.charset) for _ in range(n))

    def perturb(self, s: str, max_edits: int = 1) -> str:
        """Minimally perturb an item. Tries each edit type and returns first change."""
        s = list(s)
        for _ in range(max_edits):
            op = random.choice(["substitute", "swap", "insert", "delete"])
            if op == "substitute" and s:
                idx = random.randrange(len(s))
                candidates = [c for c in self.charset if c != s[idx]]
                if candidates:
                    s[idx] = random.choice(candidates)
            elif op == "swap" and len(s) >= 2:
                i = random.randrange(len(s) - 1)
                s[i], s[i + 1] = s[i + 1], s[i]
            elif op == "insert" and len(s) < self.max_len:
                idx = random.randrange(len(s) + 1)
                s.insert(idx, random.choice(self.charset))
            elif op == "delete" and len(s) > self.min_len:
                idx = random.randrange(len(s))
                s.pop(idx)
        return "".join(s)

    def perturb_targeted(self, s: str, classifier: Classifier,
                         target_label: bool, max_attempts: int = 200) -> str | None:
        """
        Perturb s until classifier(result) == target_label.
        Returns the perturbed string, or None if we couldn't flip it.
        """
        for _ in range(max_attempts):
            candidate = self.perturb(s)
            if candidate == s:
                continue
            try:
                if classifier.classify(candidate) == target_label:
                    return candidate
            except Exception:
                continue
        return None


ITEM_SPACES = {
    # --- Variable-length spaces ---
    "lowercase": ItemSpace("lowercase", string.ascii_lowercase, 3, 7,
                           "lowercase alphabetic strings"),
    "uppercase": ItemSpace("uppercase", string.ascii_uppercase, 3, 7,
                           "uppercase alphabetic strings"),
    "mixed_alpha": ItemSpace("mixed_alpha", string.ascii_letters, 3, 7,
                             "mixed-case alphabetic strings"),
    "numeric": ItemSpace("numeric", string.digits, 2, 5,
                         "numeric strings"),
    "alphanumeric": ItemSpace("alphanumeric",
                              string.ascii_lowercase + string.digits, 3, 7,
                              "lowercase letters and digits"),
    "with_symbols": ItemSpace("with_symbols",
                              string.ascii_lowercase + string.digits + "!@#$%&*+-=", 3, 7,
                              "letters, digits, and common symbols"),

    # --- Fixed-length spaces (eliminates length as a signal) ---
    "two_digit": ItemSpace("two_digit", string.digits, 2, 2,
                           "two-digit numbers"),
    "three_digit": ItemSpace("three_digit", string.digits, 3, 3,
                             "three-digit numbers"),
    "four_digit": ItemSpace("four_digit", string.digits, 4, 4,
                            "four-digit numbers"),
    "five_lower": ItemSpace("five_lower", string.ascii_lowercase, 5, 5,
                            "five-letter lowercase strings"),
    "four_lower": ItemSpace("four_lower", string.ascii_lowercase, 4, 4,
                            "four-letter lowercase strings"),
    "five_upper": ItemSpace("five_upper", string.ascii_uppercase, 5, 5,
                            "five-letter uppercase strings"),
}


# ============================================================================
# FEATURE AXES (declarative dependency propagation)
# ============================================================================
#
# Each feature declares the axis configurations under which it is "live"
# (takes on >=2 distinct values across samples). Axes not mentioned are
# unconstrained — the context builder freezes them to a single value.
#
# Axes:
#   case        ∈ {"lower", "upper", "mixed"}
#   charset     ∈ {"alpha", "digit", "alnum", "with_symbols"}
#   length_mode ∈ {"fixed", "variable"}

FEATURE_AXES = {
    # ---- Length / count ----
    "length":                      {"length_mode": {"variable"}},
    "count_alpha":                 {"charset": {"alnum", "with_symbols"}},
    "count_digits":                {"charset": {"digit", "alnum", "with_symbols"}},
    "count_symbols":               {"charset": {"with_symbols"}},
    "count_uppercase":             {"case": {"mixed"},
                                    "charset": {"alpha", "alnum", "with_symbols"}},
    "count_lowercase":             {"case": {"mixed"},
                                    "charset": {"alpha", "alnum", "with_symbols"}},
    "count_vowels":                {"charset": {"alpha", "alnum", "with_symbols"}},
    "count_consonants":            {"charset": {"alpha", "alnum", "with_symbols"}},
    "count_unique":                {},
    "count_spaces":                {"has_spaces": {"yes"}},
    "max_run_length":              {},
    "num_runs":                    {},
    "count_char_type_transitions": {"charset": {"alnum", "with_symbols"}},

    # ---- Character identity ----
    "first_char":                  {},
    "last_char":                   {},
    "first_char_type":             {"charset": {"alnum", "with_symbols"}},
    "last_char_type":              {"charset": {"alnum", "with_symbols"}},
    "most_common_char":            {},

    # ---- Structure ----
    "is_palindrome":               {},
    "all_chars_same":              {},
    "all_chars_unique":            {},
    "first_equals_last":           {},
    "starts_with_vowel":           {"charset": {"alpha", "alnum", "with_symbols"}},
    "ends_with_vowel":             {"charset": {"alpha", "alnum", "with_symbols"}},
    "starts_with_digit":           {"charset": {"digit", "alnum", "with_symbols"}},
    "ends_with_digit":             {"charset": {"digit", "alnum", "with_symbols"}},
    "has_digits":                  {"charset": {"alnum", "with_symbols"}},
    "has_letters":                 {"charset": {"alnum", "with_symbols"}},
    "has_symbols":                 {"charset": {"with_symbols"}},
    "has_repeated_char":           {},
    "is_alternating_case":         {"case": {"mixed"}},
    "is_alternating_char_type":    {"charset": {"alnum", "with_symbols"}},

    # ---- Alphabetic ----
    # Like digit features: property-extraction requires letters be present
    # (no-letter strings produce None and confound into "has letters" rule).
    "alpha_positions":             {"charset": {"alpha"}},
    "alpha_sum":                   {"charset": {"alpha"}},
    "alpha_product":               {"charset": {"alpha"}},
    "letters_sorted_ascending":    {"charset": {"alpha"}},
    "letters_sorted_descending":   {"charset": {"alpha"}},
    "letters_contiguous":          {"charset": {"alpha"}},
    "alpha_diffs":                 {"charset": {"alpha"}, "min_length": 3},
    "alpha_diffs_constant":        {"charset": {"alpha"}, "min_length": 3},
    "alpha_diffs_positive":        {"charset": {"alpha"}, "min_length": 3},
    "alpha_step_size":             {"charset": {"alpha"}, "min_length": 3},

    # ---- Digit ----
    # Digit features split into two kinds:
    #  (i)  counting digits within a string (fine on alnum/with_symbols)
    #  (ii) extracting properties FROM the digits (undefined if no digits,
    #       so require pure-digit context to avoid "has digits" confound)
    "digits_of":                   {"charset": {"digit"}},
    "digit_sum":                   {"charset": {"digit"}},
    # Cap digit_product inputs so products stay mentally tractable:
    # L=3 max product is 9³=729; predicates like "perfect square" stay
    # within LLM-verifiable arithmetic.
    "digit_product":               {"charset": {"digit"}, "max_length": 3},
    "num_even_digits":             {"charset": {"digit"}},
    "num_odd_digits":              {"charset": {"digit"}},
    "max_digit":                   {"charset": {"digit"}},
    "min_digit":                   {"charset": {"digit"}},
    "digit_range":                 {"charset": {"digit"}},
    "digits_sorted_ascending":     {"charset": {"digit"}},
    "digits_sorted_descending":    {"charset": {"digit"}},
    "digit_diffs":                 {"charset": {"digit"}, "min_length": 3},
    "digit_diffs_constant":        {"charset": {"digit"}, "min_length": 3},
    "digit_diffs_increasing":      {"charset": {"digit"}, "min_length": 3},
    "digits_all_same_parity":      {"charset": {"digit"}, "min_length": 3},
    "digits_alternating_parity":   {"charset": {"digit"}, "min_length": 3},
    # Cap input length so positives are common enough to sample:
    # - all-digits-even/odd at L=2 has base rate 25%; L=3 has 12.5% (both viable)
    # - all-letters-vowels at L=2 has base rate 3.7%; L=3 is 0.7% (too rare)
    "all_digits_even":             {"charset": {"digit"},
                                    "min_length": 2, "max_length": 3},
    "all_digits_odd":              {"charset": {"digit"},
                                    "min_length": 2, "max_length": 3},
    "all_letters_vowels":          {"charset": {"alpha"},
                                    "min_length": 2, "max_length": 2},
    "all_letters_consonants":      {"charset": {"alpha"},
                                    "min_length": 2, "max_length": 3},
    # "Opposite" implies exactly 2 digits — pin length to 2.
    "opposite_parity_digits":      {"charset": {"digit"},
                                    "min_length": 2, "max_length": 2,
                                    "length_mode": {"fixed"}},

    # ---- Numeric (pure-digit string-as-number) ----
    "numeric_value":               {"charset": {"digit"}},
    "is_prime_number":             {"charset": {"digit"}},
    "is_perfect_square":           {"charset": {"digit"}},
    "is_fibonacci":                {"charset": {"digit"}},
    # Cap num_divisors inputs: on 2-digit numbers (0-99), divisors ≤ 12,
    # values are within countable range for a solver.
    "num_divisors":                {"charset": {"digit"}, "max_length": 2},
    "is_palindrome_number":        {"charset": {"digit"}},
    # Cap input range so binary values stay small (≤ 7 bits for 2-digit input).
    # LLMs can't reliably count digits past ~5, so we keep feature values tiny.
    "binary_digit_count":          {"charset": {"digit"}, "max_length": 2},
    "binary_length":               {"charset": {"digit"}, "max_length": 2},

    # ---- Encoding ----
    "ascii_sum":                   {},
    "scrabble_score":              {"charset": {"alpha"}},
    "morse_length":                {"charset": {"alpha", "digit", "alnum", "with_symbols"}},
    # Roman numerals are only defined for 1..3999, so cap at 3-digit strings.
    "roman_numeral_length":        {"charset": {"digit"}, "max_length": 3},
    "count_enclosed_regions":      {},
    "count_curved_letters":        {"charset": {"alpha", "alnum", "with_symbols"},
                                    "case": {"lower", "mixed"}},
    "count_ascenders":             {"charset": {"alpha", "alnum", "with_symbols"},
                                    "case": {"lower", "mixed"}},
    "count_descenders":            {"charset": {"alpha", "alnum", "with_symbols"},
                                    "case": {"lower", "mixed"}},
    "count_symmetric_chars":       {},

    # ---- Pattern ----
    "cv_pattern":                  {"charset": {"alpha", "alnum", "with_symbols"}},
    "type_pattern":                {"charset": {"alnum", "with_symbols"}},
    "case_pattern":                {"case": {"mixed"}},
    # Periodicity: items need length large enough for period to be
    # meaningfully less than length. Force variable length with min=4.
    "is_periodic":                 {"length_mode": {"variable"}, "min_length": 4},
    "shortest_period":             {"length_mode": {"variable"}, "min_length": 4},
    "cv_pattern_is_palindrome":    {"charset": {"alpha", "alnum", "with_symbols"}},
    "has_repeated_bigram":         {},

    # ---- Ratio ----
    "vowel_ratio":                 {"charset": {"alpha", "alnum", "with_symbols"}},
    "digit_letter_ratio":          {"charset": {"alnum", "with_symbols"}},
    "uppercase_ratio":             {"case": {"mixed"},
                                    "charset": {"alpha", "alnum", "with_symbols"}},
    "unique_ratio":                {},
}


# Charset constructors given a case
_CASE_ALPHA = {
    "lower": string.ascii_lowercase,
    "upper": string.ascii_uppercase,
    "mixed": string.ascii_letters,
}


def _charset_chars(charset_kind: str, case: str) -> str:
    """Build the actual charset string for a given (charset, case)."""
    if charset_kind == "alpha":
        return _CASE_ALPHA[case]
    if charset_kind == "digit":
        return string.digits
    if charset_kind == "alnum":
        return _CASE_ALPHA[case] + string.digits
    if charset_kind == "with_symbols":
        return _CASE_ALPHA[case] + string.digits + "!@#$%&*+-="
    raise ValueError(f"unknown charset kind: {charset_kind}")


@dataclass
class ConceptContext:
    """
    Per-problem frozen invariants. Axes not required by the classifier
    are pinned to a single value so they can't miscue the solver.
    """
    case: str                       # "lower" | "upper" | "mixed"
    charset: str                    # "alpha" | "digit" | "alnum" | "with_symbols"
    length_mode: str                # "fixed" | "variable"
    length: int | tuple[int, int]   # int if fixed, (min,max) if variable

    def name(self) -> str:
        """Short string identifying this context (for logs / CSV)."""
        if self.length_mode == "fixed":
            lenstr = f"L{self.length}"
        else:
            lenstr = f"L{self.length[0]}-{self.length[1]}"
        return f"{self.case}_{self.charset}_{lenstr}"

    def description(self) -> str:
        """Human-readable description of what items look like."""
        parts = []
        if self.case == "lower":
            parts.append("lowercase")
        elif self.case == "upper":
            parts.append("uppercase")
        else:
            parts.append("mixed-case")
        if self.charset == "alpha":
            parts.append("letters")
        elif self.charset == "digit":
            parts = ["digit strings"]  # "lowercase digit strings" is nonsense
        elif self.charset == "alnum":
            parts.append("letters and digits")
        else:
            parts.append("letters, digits, and symbols")
        if self.length_mode == "fixed":
            parts.append(f"({self.length} characters)")
        else:
            parts.append(f"({self.length[0]}-{self.length[1]} characters)")
        return " ".join(parts)

    def as_item_space(self) -> ItemSpace:
        """Adapt to the existing ItemSpace interface so downstream code works unchanged."""
        chars = _charset_chars(self.charset, self.case)
        if self.length_mode == "fixed":
            return ItemSpace(self.name(), chars, self.length, self.length,
                             self.description())
        else:
            lo, hi = self.length
            return ItemSpace(self.name(), chars, lo, hi, self.description())


# ============================================================================
# CONTEXT PROPAGATION & BUILDING
# ============================================================================

def propagate_axes(classifier: Classifier,
                   feature_axes: dict = FEATURE_AXES) -> dict:
    """
    Intersect axis requirements across the classifier's features.
    Returns dict: axis_name → set of acceptable values OR int (for min_length).
    Axes absent from the result are unconstrained.
    """
    constraints: dict = {}
    for fname in classifier.feature_names:
        reqs = feature_axes.get(fname, {})
        for axis, accepted in reqs.items():
            if axis == "min_length":
                # Scalar — take the MAX (most restrictive lower bound)
                constraints[axis] = max(constraints.get(axis, 0), int(accepted))
            elif axis == "max_length":
                # Scalar — take the MIN (most restrictive upper bound)
                prior = constraints.get(axis, 10**9)
                constraints[axis] = min(prior, int(accepted))
            else:
                if axis in constraints:
                    constraints[axis] = constraints[axis] & set(accepted)
                else:
                    constraints[axis] = set(accepted)
    return constraints


# Charset preference order: tightest first (smallest hypothesis space)
_CHARSET_ORDER = ["alpha", "digit", "alnum", "with_symbols"]
_CASE_DEFAULT_ORDER = ["lower", "upper", "mixed"]


def build_context(
    classifier: Classifier,
    feature_axes: dict = FEATURE_AXES,
    default_length: int = 5,
    default_length_range: tuple[int, int] = (3, 7),
    empirical_check: bool = True,
    min_each_label: int = 8,
) -> ConceptContext | None:
    """
    Build the tightest per-problem context consistent with the classifier's
    requirements, with an empirical safety net.

    Steps:
      1. Propagate axis constraints from the classifier's features.
      2. Pick the tightest value for each unconstrained axis.
      3. Empirically verify both labels are reachable under this context;
         if not, relax the most-constrained axis and retry.
    """
    constraints = propagate_axes(classifier, feature_axes)

    allowed_case = constraints.get("case", set(_CASE_DEFAULT_ORDER))
    allowed_charset = constraints.get("charset", set(_CHARSET_ORDER))
    allowed_length_mode = constraints.get("length_mode", {"fixed", "variable"})
    min_length_req = constraints.get("min_length", 0)      # scalar
    max_length_req = constraints.get("max_length", 10**9)  # scalar

    def pick_case(allowed):
        for c in _CASE_DEFAULT_ORDER:
            if c in allowed:
                return c
        return next(iter(allowed))

    def pick_charset(allowed):
        for c in _CHARSET_ORDER:
            if c in allowed:
                return c
        return next(iter(allowed))

    def make_ctx(case, charset, length_mode):
        # Pick length honoring both min_length_req and max_length_req.
        if length_mode == "fixed":
            baseline = 4 if charset == "digit" else default_length
            length = max(min_length_req, min(baseline, max_length_req))
            length = max(1, length)
        else:
            if charset == "digit":
                lo_base, hi_base = 2, 5
            else:
                lo_base, hi_base = default_length_range
            lo = max(lo_base, min_length_req)
            hi = min(hi_base, max_length_req)
            if hi < lo:
                hi = lo
            if hi < lo + 2:
                hi = min(lo + 2, max_length_req)
            length = (lo, max(lo, hi))
        return ConceptContext(case=case, charset=charset,
                              length_mode=length_mode, length=length)

    # --- Initial tight context ---
    case = pick_case(allowed_case)
    charset = pick_charset(allowed_charset)
    length_mode = "fixed" if "fixed" in allowed_length_mode else "variable"
    ctx = make_ctx(case, charset, length_mode)

    # --- Empirical check ---
    # Skip if the classifier has a concept-aware positive generator: we
    # KNOW we can construct positives, even if uniform sampling base rate
    # is too low to satisfy min_each_label.
    has_generator = (
        classifier.description in POSITIVE_GENERATORS
        or classifier.description in POSITIVE_BIASED_CHARSETS
    )
    if not empirical_check or has_generator:
        return ctx

    def check(ctx):
        space = ctx.as_item_space()
        pos = neg = 0
        for _ in range(300):
            item = space.sample()
            try:
                r = classifier.classify(item)
            except Exception:
                continue
            if r:
                pos += 1
            else:
                neg += 1
            if pos >= min_each_label and neg >= min_each_label:
                return True
        return pos >= min_each_label and neg >= min_each_label

    if check(ctx):
        return ctx

    # --- Relaxation ladder ---
    # Try length_mode=variable first (cheap relaxation)
    if length_mode == "fixed" and "variable" in allowed_length_mode:
        ctx2 = make_ctx(case, charset, "variable")
        if check(ctx2):
            return ctx2

    # Try broader charsets
    for c in _CHARSET_ORDER:
        if c == charset or c not in allowed_charset:
            continue
        ctx2 = make_ctx(case, c, length_mode)
        if check(ctx2):
            return ctx2
        if "variable" in allowed_length_mode:
            ctx2 = make_ctx(case, c, "variable")
            if check(ctx2):
                return ctx2

    # Last resort: classifier is hard to satisfy; caller will filter it out
    return None

# Which item spaces are compatible with which feature categories.
# Fixed-length spaces intentionally EXCLUDE "Length/count" since all items
# have the same length, making length-based features uninformative as
# discriminators (the solver can see they're all the same length).
SPACE_COMPATIBILITY = {
    "lowercase": {"Length/count", "Character identity", "Structure", "Alphabetic",
                  "Pattern", "Encoding"},
    "uppercase": {"Length/count", "Character identity", "Structure", "Alphabetic",
                  "Pattern", "Encoding"},
    "mixed_alpha": {"Length/count", "Character identity", "Structure", "Alphabetic",
                    "Pattern", "Encoding"},
    "numeric": {"Length/count", "Character identity", "Structure", "Digit",
                "Numeric", "Pattern", "Encoding"},
    "alphanumeric": {"Length/count", "Character identity", "Structure", "Alphabetic",
                     "Digit", "Pattern", "Ratio", "Encoding"},
    "with_symbols": {"Length/count", "Character identity", "Structure", "Alphabetic",
                     "Digit", "Pattern", "Ratio", "Encoding"},

    # Fixed-length spaces: Length/count stays because count_vowels etc. are
    # still meaningful; only `length` itself is trivially constant (but
    # base-rate filtering will kill "length == N" classifiers here anyway).
    "two_digit": {"Length/count", "Digit", "Numeric", "Encoding"},
    "three_digit": {"Length/count", "Digit", "Numeric", "Encoding"},
    "four_digit": {"Length/count", "Digit", "Numeric", "Encoding"},
    "five_lower": {"Length/count", "Character identity", "Structure", "Alphabetic",
                   "Pattern", "Encoding"},
    "four_lower": {"Length/count", "Character identity", "Structure", "Alphabetic",
                   "Pattern", "Encoding"},
    "five_upper": {"Length/count", "Character identity", "Structure", "Alphabetic",
                   "Pattern", "Encoding"},
}


# ============================================================================
# CLASSIFIER LIBRARY
# ============================================================================

# Features that typically produce values in small ranges (0-3) for short strings.
# Predicates like divisible_by(5) or is_prime degenerate on these.
SMALL_RANGE_FEATURES = {
    "count_symbols", "count_spaces", "max_run_length",
    "count_char_type_transitions", "count_digits", "count_uppercase",
    "digit_range", "num_even_digits", "num_odd_digits",
    "binary_digit_count", "shortest_period",
}

# Features that produce values large enough for modular/prime predicates
LARGE_RANGE_FEATURES = {
    "length", "count_alpha", "count_lowercase", "count_consonants",
    "count_vowels", "count_unique", "num_runs",
    "alpha_sum", "alpha_product", "digit_sum", "digit_product",
    "ascii_sum", "scrabble_score", "morse_length",
    "roman_numeral_length", "num_divisors", "binary_length",
    "max_digit", "min_digit",
}


# Classifier descriptions that are trivially obvious for LLMs and
# uninteresting as Bongard problems (immediately visible from the items).
# These are mostly "presence/absence" checks that a solver spots instantly.
TRIVIAL_CONCEPTS = {
    # Presence/absence of character classes — trivially visible
    "count vowels > 0",
    "count consonants > 0",
    "count digits > 0",
    "count digits equals 0",
    "count alpha > 0",
    "count alpha equals 0",
    "count lowercase > 0",
    "count lowercase equals 0",
    "count uppercase > 0",
    "count uppercase equals 0",
    "count symbols > 0",
    "count symbols equals 0",
    "has digits",
    "has letters",
    "has symbols",
    "has repeated char",
    # Length-based trivialities
    "length > 0",
    "length > 1",
    "length equals 0",
    "length equals 1",
    "count unique > 0",
    "count unique > 1",
    # Degenerate digit rules
    "min digit > 0",        # "no zeros"
    "min digit equals 0",   # "has a zero"
    "max digit > 0",        # "has any non-zero"
}


# Features that are DERIVED — require multi-step computation (not scannable).
# Only allow simple thresholds (equals, gt) on these — never modular
# arithmetic or prime tests, which compound abstraction on abstraction and
# yield concepts the solver can't plausibly data-drive.
DERIVED_FEATURES = {
    "alpha_sum",           # requires a=1,b=2,... lookup + summation
    "alpha_product",       # same lookup + product
    "ascii_sum",           # requires ASCII table knowledge
    "scrabble_score",      # requires Scrabble value table
    "morse_length",        # requires Morse table
    "roman_numeral_length",# requires Roman numeral conversion
    "num_divisors",        # requires factoring
    "binary_digit_count",  # requires base conversion
    "binary_length",       # requires base conversion
    "is_prime_number",     # requires primality (but it's already bool)
    "is_perfect_square",   # same
    "is_fibonacci",        # same
    "is_palindrome_number",# same
}


# Classifiers that are STRUCTURALLY equivalent to a simpler rule —
# not an artifact of sampling but a mathematical identity. Never emit these.
# Classifier descriptions use SPACES (not underscores) — match those here.
REDUCIBLE_CLASSIFIERS = {
    # --- digit_product / min_digit / max_digit identities ---
    "digit product equals 0",   # ≡ contains a 0
    "digit product > 0",        # ≡ no 0s
    "alpha product equals 0",   # impossible (letters map to ≥1)
    "min digit equals 0",       # ≡ contains a 0
    "min digit > 0",            # ≡ no 0s
    "max digit > 0",            # ≡ has any non-zero digit (trivial)

    # --- "count of X = 0" — absence-of-X rules that have a cleaner boolean
    # expression. Each is either a first-class feature now, or covered by
    # SALIENT_ALTERNATIVES / TRIVIAL_CONCEPTS. ---
    "num odd digits equals 0",   # ≡ all_digits_even (first-class)
    "num even digits equals 0",  # ≡ all_digits_odd  (first-class)
    "count vowels equals 0",     # ≡ all_letters_consonants (first-class)
    "count consonants equals 0", # ≡ all_letters_vowels     (first-class)

    # --- Other = 0 degeneracies ---
    # digit_range = 0 means min = max = same digit, i.e. all chars equal
    # (on digit-only context). Same as `all_chars_same`.
    "digit range equals 0",
    # digit_sum = 0 is only satisfied by "000...0" — a single item, never
    # a useful concept. Base rate filter kills it, but make it explicit.
    "digit sum equals 0",
    # char-type transitions = 0 means all chars share a type. Covered by
    # the "homogeneous char type" salient alternative.
    "count char type transitions equals 0",

    # --- Absence-of-X rules dressed as numeric equality ---
    "max run length equals 0",
    "max run length equals 1",  # ≡ no adjacent repeats
    "max run length > 0",       # trivial for non-empty

    "num runs equals 1",        # ≡ all chars identical
    "num runs > 0",             # trivial

    "shortest period equals 1", # ≡ all chars identical
    "shortest period > 0",      # trivial

    "count unique equals 1",    # ≡ all chars identical
}


def build_classifier_library() -> list[Classifier]:
    """
    Programmatically generate classifiers by composing features + predicates.
    Returns depth-0 and depth-1 classifiers.

    Predicate types generated:
      - Bool features: used directly
      - Int features (large range): is_even, is_odd, is_prime, divisible_by(k),
                                    is_perfect_square, equals(k), gt(k)
      - Int features (small range): equals(k), gt(k), is_zero, is_nonzero
                                    (NO parity/modular — they degenerate)
      - Depth 1: AND of two bool features, int comparisons (gt, eq)

    Also blocks:
      - TRIVIAL_CONCEPTS: trivially obvious rules (presence/absence)
      - REDUCIBLE_CLASSIFIERS: structurally equivalent to simpler rules
    """
    classifiers = []
    seen_descriptions = set()

    def add(clf: Classifier):
        if clf.description in seen_descriptions:
            return
        if clf.description in TRIVIAL_CONCEPTS:
            return
        if clf.description in REDUCIBLE_CLASSIFIERS:
            return
        seen_descriptions.add(clf.description)
        classifiers.append(clf)

    def _diff_label(cat):
        diff = CATEGORY_DIFFICULTY.get(cat, 3)
        return {1: "easy", 2: "medium", 3: "hard", 4: "very_hard"}.get(diff, "hard")

    # --- Depth 0: bool features used directly ---
    bool_features = [
        (name, fn) for name, (fn, rtype, cat) in FEATURE_REGISTRY.items()
        if rtype == bool
    ]
    for name, fn in bool_features:
        cat = FEATURE_REGISTRY[name][2]
        add(Classifier(
            description=name.replace("_", " "),
            classify=fn,
            depth=0, feature_names=[name], difficulty_hint=_diff_label(cat),
        ))

    # --- Depth 0: int features + predicates ---
    int_features = [
        (name, fn) for name, (fn, rtype, cat) in FEATURE_REGISTRY.items()
        if rtype == int
    ]

    for name, fn in int_features:
        cat = FEATURE_REGISTRY[name][2]
        dl = _diff_label(cat)
        hr = name.replace("_", " ")
        is_large = name in LARGE_RANGE_FEATURES
        is_small = name in SMALL_RANGE_FEATURES

        # --- Predicates for ALL int features ---
        # NOTE: `> K` predicates removed because the threshold isn't uniquely
        # identifiable from examples. Positives with values {3,4,5} vs
        # negatives with {0,1,2} could equally be "> 2", "≥ 3", or "≠ 0..2".
        # Only emit predicates whose truth is point-wise verifiable on each
        # item without knowing a threshold.

        # equals(k) for small specific values
        for k in [0, 1, 2, 3, 4, 5]:
            add(Classifier(
                description=f"{hr} equals {k}",
                classify=lambda s, f=fn, k=k: f(s) is not None and f(s) == k,
                depth=0, feature_names=[name], difficulty_hint=dl,
            ))

        # --- Predicates for large-range features ---
        # DERIVED features (num_divisors, ascii_sum, morse_length, etc.) get
        # only simple thresholds (equals, gt) — already emitted above. They
        # skip parity, prime, perfect-square, and modular predicates because
        # layering abstraction on abstraction produces concepts that are
        # impossible to cue from item inspection.
        is_derived = name in DERIVED_FEATURES
        if is_large and not is_derived:
            # is_even / is_odd
            add(Classifier(
                description=f"{hr} is even",
                classify=lambda s, f=fn: is_even(f(s)),
                depth=0, feature_names=[name], difficulty_hint=dl,
            ))
            add(Classifier(
                description=f"{hr} is odd",
                classify=lambda s, f=fn: is_odd(f(s)),
                depth=0, feature_names=[name], difficulty_hint=dl,
            ))

            # is_prime (skip features that are too large for useful primes)
            if name not in ("ascii_sum", "alpha_product"):
                add(Classifier(
                    description=f"{hr} is prime",
                    classify=lambda s, f=fn: f(s) is not None and _is_prime(f(s)),
                    depth=0, feature_names=[name], difficulty_hint=dl,
                ))

            # is_perfect_square
            add(Classifier(
                description=f"{hr} is a perfect square",
                classify=lambda s, f=fn: (
                    f(s) is not None and f(s) >= 0
                    and int(math.isqrt(f(s))) ** 2 == f(s)
                ),
                depth=0, feature_names=[name], difficulty_hint=dl,
            ))

            # divisible_by(k) — point-wise verifiable once the value is known
            for k in [3, 4, 5, 7]:
                add(Classifier(
                    description=f"{hr} divisible by {k}",
                    classify=lambda s, f=fn, k=k: f(s) is not None and f(s) % k == 0,
                    depth=0, feature_names=[name], difficulty_hint=dl,
                ))

            # Extra equals(k) for larger values in this feature's natural range
            for k in [6, 7, 8, 10, 12]:
                add(Classifier(
                    description=f"{hr} equals {k}",
                    classify=lambda s, f=fn, k=k: f(s) is not None and f(s) == k,
                    depth=0, feature_names=[name], difficulty_hint=dl,
                ))

        # DERIVED features get equals(k) at a broader range of values.
        # NO gt predicates — threshold non-identifiable from examples.
        if is_derived:
            for k in [4, 5, 6, 7, 8, 10, 12]:
                add(Classifier(
                    description=f"{hr} equals {k}",
                    classify=lambda s, f=fn, k=k: f(s) is not None and f(s) == k,
                    depth=0, feature_names=[name], difficulty_hint=dl,
                ))

    # --- Depth 1: AND of two bool features from different categories ---
    for i, (name1, fn1) in enumerate(bool_features):
        cat1 = FEATURE_REGISTRY[name1][2]
        for name2, fn2 in bool_features[i + 1:]:
            cat2 = FEATURE_REGISTRY[name2][2]
            if cat1 == cat2:
                continue
            diff = max(CATEGORY_DIFFICULTY.get(cat1, 3), CATEGORY_DIFFICULTY.get(cat2, 3))
            dl = {1: "easy", 2: "medium", 3: "hard", 4: "very_hard"}.get(diff, "hard")
            add(Classifier(
                description=f"{name1.replace('_', ' ')} AND {name2.replace('_', ' ')}",
                classify=lambda s, f1=fn1, f2=fn2: f1(s) and f2(s),
                depth=1, feature_names=[name1, name2], difficulty_hint=dl,
            ))

    # --- Depth 1: AND of bool + int predicate ---
    # These are interesting because the bool feature is "noticeable" and the
    # int predicate adds a hidden constraint.
    _select_bool = [
        (n, f) for n, f in bool_features
        if n in ("is_palindrome", "first_equals_last", "starts_with_vowel",
                 "ends_with_vowel", "letters_contiguous", "all_chars_unique",
                 "is_periodic", "has_repeated_char", "letters_sorted_ascending",
                 "digits_sorted_ascending", "digits_alternating_parity")
    ]
    _select_int_preds = []
    for name, fn in int_features:
        if name in LARGE_RANGE_FEATURES:
            hr = name.replace("_", " ")
            _select_int_preds.append((
                f"{hr} is even",
                lambda s, f=fn: is_even(f(s)),
                name,
            ))
            _select_int_preds.append((
                f"{hr} is odd",
                lambda s, f=fn: is_odd(f(s)),
                name,
            ))

    for bool_name, bool_fn in _select_bool:
        for int_desc, int_fn, int_fname in _select_int_preds[:20]:  # limit combos
            bcat = FEATURE_REGISTRY[bool_name][2]
            icat = FEATURE_REGISTRY[int_fname][2]
            if bcat == icat:
                continue
            diff = max(CATEGORY_DIFFICULTY.get(bcat, 3), CATEGORY_DIFFICULTY.get(icat, 3))
            dl = {1: "easy", 2: "medium", 3: "hard", 4: "very_hard"}.get(diff, "hard")
            desc = f"{bool_name.replace('_', ' ')} AND {int_desc}"
            add(Classifier(
                description=desc,
                classify=lambda s, bf=bool_fn, ifn=int_fn: bf(s) and ifn(s),
                depth=1, feature_names=[bool_name, int_fname], difficulty_hint=dl,
            ))

    # --- Depth 1: int feature comparisons ---
    comparison_pairs = [
        ("count_vowels", "count_consonants"),
        ("count_uppercase", "count_lowercase"),
        ("count_digits", "count_alpha"),
        ("num_even_digits", "num_odd_digits"),
        ("count_ascenders", "count_descenders"),
        ("count_curved_letters", "count_symmetric_chars"),
        ("digit_sum", "length"),
        ("digit_sum", "count_curved_letters"),
        ("digit_sum", "count_vowels"),
        ("max_digit", "min_digit"),
        ("count_enclosed_regions", "length"),
        ("count_enclosed_regions", "count_vowels"),
        ("scrabble_score", "morse_length"),
        ("scrabble_score", "alpha_sum"),
        ("count_vowels", "length"),
        ("alpha_sum", "digit_sum"),
        ("count_unique", "length"),
    ]
    for name1, name2 in comparison_pairs:
        if name1 not in FEATURE_REGISTRY or name2 not in FEATURE_REGISTRY:
            continue
        fn1 = FEATURE_REGISTRY[name1][0]
        fn2 = FEATURE_REGISTRY[name2][0]
        cat1 = FEATURE_REGISTRY[name1][2]
        cat2 = FEATURE_REGISTRY[name2][2]
        diff = max(CATEGORY_DIFFICULTY.get(cat1, 3), CATEGORY_DIFFICULTY.get(cat2, 3))
        dl = {1: "easy", 2: "medium", 3: "hard", 4: "very_hard"}.get(diff, "hard")

        # Only equality comparisons — gt/gte have the same threshold-
        # identifiability problem as single-feature `> K` predicates.
        for op_name, op_sym, op_fn in [
            ("eq", "==", lambda a, b: a == b),
        ]:
            desc = f"{name1.replace('_', ' ')} {op_sym} {name2.replace('_', ' ')}"
            add(Classifier(
                description=desc,
                classify=lambda s, f1=fn1, f2=fn2, op=op_fn: (
                    f1(s) is not None and f2(s) is not None and op(f1(s), f2(s))
                ),
                depth=1, feature_names=[name1, name2], difficulty_hint=dl,
            ))

    # (Trivial/reducible filtering now happens inside `add()` at emission time.)
    return classifiers


# ============================================================================
# BASE RATE FILTERING
# ============================================================================

def compute_base_rate(
    classifier: Classifier,
    item_space: ItemSpace,
    n_samples: int = 200,
) -> float | None:
    """
    Estimate what fraction of random items satisfy the classifier.
    Returns None if the classifier errors on most items.
    """
    pos_count = 0
    valid_count = 0
    for _ in range(n_samples):
        item = item_space.sample()
        try:
            result = classifier.classify(item)
            valid_count += 1
            if result:
                pos_count += 1
        except Exception:
            continue

    if valid_count < n_samples * 0.5:
        return None  # too many errors — classifier doesn't work for this space

    return pos_count / valid_count


def filter_classifiers_by_base_rate(
    classifiers: list[Classifier],
    item_space: ItemSpace,
    min_rate: float = 0.15,
    max_rate: float = 0.85,
    n_samples: int = 200,
) -> list[Classifier]:
    """
    Remove classifiers that are trivially satisfied or trivially violated.
    Keep only those with base rate in [min_rate, max_rate].
    """
    filtered = []
    for clf in classifiers:
        rate = compute_base_rate(clf, item_space, n_samples)
        if rate is not None and min_rate <= rate <= max_rate:
            clf._base_rate = rate  # stash for diagnostics
            filtered.append(clf)
    return filtered


# ============================================================================
# CLOSE-NEGATIVE GENERATION
# ============================================================================

def generate_close_negative_for_and(
    positive: str,
    conjunct_classifiers: list[Callable[[str], bool]],
    item_space: ItemSpace,
    max_attempts: int = 300,
) -> tuple[str, int] | None:
    """
    For an AND-classifier with known conjuncts, generate a close negative
    that breaks exactly one conjunct.

    Returns (negative_item, which_conjunct_was_broken) or None.
    """
    # Try to break each conjunct in turn
    conjunct_to_break = random.randrange(len(conjunct_classifiers))

    for _ in range(max_attempts):
        candidate = item_space.perturb(positive)
        if candidate == positive:
            continue

        try:
            results = [c(candidate) for c in conjunct_classifiers]
        except Exception:
            continue

        # We want exactly one conjunct broken
        n_true = sum(results)
        if n_true == len(results) - 1 and not results[conjunct_to_break]:
            return candidate, conjunct_to_break

    # Fallback: accept any perturbation that breaks at least one conjunct
    for _ in range(max_attempts):
        candidate = item_space.perturb(positive)
        if candidate == positive:
            continue
        try:
            results = [c(candidate) for c in conjunct_classifiers]
        except Exception:
            continue
        if not all(results):
            broken = next(i for i, r in enumerate(results) if not r)
            return candidate, broken

    return None


def generate_close_negative_simple(
    positive: str,
    classifier: Classifier,
    item_space: ItemSpace,
    max_attempts: int = 300,
) -> str | None:
    """Generate a close negative by perturbing a positive until it flips."""
    for _ in range(max_attempts):
        candidate = item_space.perturb(positive)
        if candidate == positive:
            continue
        try:
            if not classifier.classify(candidate):
                return candidate
        except Exception:
            continue
    return None


# ============================================================================
# AMBIGUITY CHECK
# ============================================================================

def _are_context_equivalent(
    clf_a: Classifier,
    clf_b: Classifier,
    item_space: ItemSpace | None,
    n_samples: int = 150,
) -> bool:
    """
    Return True if two classifiers produce identical outputs on random items
    from the item_space. This catches derived-from aliases, e.g. on pure-alpha
    L=3, "all letters consonants" ≡ "count vowels equals 0" — both return
    True iff the string has no vowels.
    """
    if item_space is None:
        return False
    for _ in range(n_samples):
        item = item_space.sample()
        try:
            if clf_a.classify(item) != clf_b.classify(item):
                return False
        except Exception:
            continue
    return True


def check_ambiguity(
    positives: list[str],
    negatives: list[str],
    target_classifier: Classifier,
    all_classifiers: list[Classifier],
    item_space: ItemSpace | None = None,
) -> list[str]:
    """
    Find simpler classifiers that ALSO perfectly separate these examples.
    Returns list of ambiguous classifier descriptions (empty = unambiguous).

    A simpler alternative is considered a REAL ambiguity only if it's also
    semantically distinct from the target on the context. An alternative
    that agrees with the target on every random sample from the context is
    an alias (e.g. "count vowels = 0" ≡ "all letters consonants" on pure
    alpha) and doesn't constitute ambiguity.
    """
    all_items = positives + negatives
    true_labels = [True] * len(positives) + [False] * len(negatives)

    target_max_diff = max(
        CATEGORY_DIFFICULTY.get(
            FEATURE_REGISTRY.get(f, (None, None, "Encoding"))[2], 4
        )
        for f in target_classifier.feature_names
    )

    ambiguous = []
    for clf in all_classifiers:
        if clf.description == target_classifier.description:
            continue

        clf_max_diff = max(
            CATEGORY_DIFFICULTY.get(
                FEATURE_REGISTRY.get(f, (None, None, "Encoding"))[2], 4
            )
            for f in clf.feature_names
        )
        is_simpler = (
            clf_max_diff < target_max_diff
            or (clf_max_diff == target_max_diff and clf.depth < target_classifier.depth)
        )
        if not is_simpler:
            continue

        try:
            predictions = [clf.classify(item) for item in all_items]
        except Exception:
            continue

        if predictions != true_labels:
            continue

        # Candidate separator — check if it's just a context-equivalent alias
        # of the target. If so, skip.
        if _are_context_equivalent(clf, target_classifier, item_space):
            continue

        ambiguous.append(clf.description)

    return ambiguous


# ============================================================================
# SALIENT ALTERNATIVES (ambiguity check against simple rules)
# ============================================================================
#
# These are the "first rules a solver tries". If any of them perfectly
# separates the examples, the problem is ambiguous regardless of whether
# the alternative is in the classifier library.

def _char_type(c: str) -> str:
    if c.isdigit():
        return "d"
    if c.isalpha():
        return "a"
    if c.isspace():
        return "w"
    return "s"


SALIENT_ALTERNATIVES = [
    # --- Presence / absence of character classes ---
    ("has any digits",             lambda s: any(c.isdigit() for c in s)),
    ("no digits",                  lambda s: not any(c.isdigit() for c in s)),
    ("has any letters",            lambda s: any(c.isalpha() for c in s)),
    ("no letters",                 lambda s: not any(c.isalpha() for c in s)),
    ("has any symbols",            lambda s: any(not c.isalnum() and not c.isspace() for c in s)),
    ("no symbols",                 lambda s: all(c.isalnum() or c.isspace() for c in s)),
    ("has uppercase",              lambda s: any(c.isupper() for c in s)),
    ("all non-upper",              lambda s: not any(c.isupper() for c in s)),
    ("has lowercase",              lambda s: any(c.islower() for c in s)),
    ("all non-lower",              lambda s: not any(c.islower() for c in s)),

    # --- Homogeneity of character type ---
    # "All characters are the same type" — equivalent to count_char_type_
    # transitions == 0, but much more obvious to a solver.
    ("homogeneous char type",
     lambda s: bool(s) and len({_char_type(c) for c in s}) == 1),
    # "Contains both letters and digits" — equivalent to having a transition
    # when the charset is alpha+digit. Catches transitions>0 on alnum.
    ("has both letters and digits",
     lambda s: any(c.isalpha() for c in s) and any(c.isdigit() for c in s)),

    # --- Uniqueness / repetition ---
    ("all chars unique",           lambda s: len(set(s)) == len(s)),
    ("has repeated char",          lambda s: bool(s) and len(set(s)) < len(s)),
    ("no adjacent repeats",
     lambda s: all(s[i] != s[i+1] for i in range(len(s)-1)) if s else True),
    ("has adjacent repeats",
     lambda s: any(s[i] == s[i+1] for i in range(len(s)-1)) if s else False),

    # --- All-digits-even / all-digits-odd (cleaner than num_X_digits = 0).
    # Descriptions match the FEATURE_LABELS for these features so the
    # salient-check's self-matching guard recognizes them as the target rule. ---
    ("all digits are even",
     lambda s: any(c.isdigit() for c in s)
               and all((not c.isdigit()) or int(c) % 2 == 0 for c in s)),
    ("all digits are odd",
     lambda s: any(c.isdigit() for c in s)
               and all((not c.isdigit()) or int(c) % 2 == 1 for c in s)),

    # --- All-vowels / all-consonants (cleaner than count_X = 0 on alpha) ---
    ("all letters are vowels",
     lambda s: any(c.isalpha() for c in s)
               and all((not c.isalpha()) or c in "aeiouAEIOU" for c in s)),
    ("all letters are consonants",
     lambda s: any(c.isalpha() for c in s)
               and all((not c.isalpha()) or (c.isalpha() and c not in "aeiouAEIOU") for c in s)),

    # --- Structure ---
    ("is palindrome",              lambda s: bool(s) and s == s[::-1]),
    ("all chars same",             lambda s: bool(s) and len(set(s)) == 1),
    ("first equals last",          lambda s: bool(s) and s[0] == s[-1]),

    # --- First/last character ---
    ("starts with vowel",          lambda s: bool(s) and s[0] in "aeiouAEIOU"),
    ("ends with vowel",            lambda s: bool(s) and s[-1] in "aeiouAEIOU"),
    ("starts with digit",          lambda s: bool(s) and s[0].isdigit()),
    ("ends with digit",            lambda s: bool(s) and s[-1].isdigit()),
    ("first and last same type",
     lambda s: bool(s) and _char_type(s[0]) == _char_type(s[-1])),

    # --- Zero-count variants (often accidentally perfect) ---
    ("no vowels",                  lambda s: not any(c in "aeiouAEIOU" for c in s)),
    ("has no repeated char",       lambda s: len(set(s)) == len(s)),
]


# Per-target semantic aliases: salient alternatives that mean the same
# thing as the target on the target's native context. We SKIP these in
# the ambiguity check since they're not a real confound.
SEMANTIC_ALIASES = {
    "all letters consonants": {"no vowels"},
    "all letters vowels":     set(),
    "all digits even":        set(),
    "all digits odd":         set(),
    # all_chars_unique vs has_repeated_char are strictly complementary,
    # and count_unique-based rules often coincide on random samples.
    "all chars unique":       {"has repeated char", "has no repeated char",
                               "count unique is odd", "count unique is prime",
                               "count unique is a perfect square"},
    "has repeated char":      {"all chars unique", "has no repeated char"},
    # Similar for is_palindrome — first-equals-last coincides often, but
    # first_equals_last IS strictly weaker (1432 → not palindrome but fel).
    # Leaving that one as a legit ambiguity check.
}


def find_salient_ambiguities(positives: list[str],
                             negatives: list[str],
                             target_classifier: Classifier | None = None) -> list[str]:
    """
    Check whether any simple/salient rule also perfectly separates the examples.
    Returns descriptions of matches. Empty = no salient ambiguity.
    Checks BOTH directions (rule ↔ !rule) since either orientation miscues.
    """
    all_items = positives + negatives
    true_labels = [True] * len(positives) + [False] * len(negatives)
    flipped = [not x for x in true_labels]

    # Build aliases so a salient alternative doesn't match the target when
    # they express the same concept. Includes both exact-name aliases (the
    # description or its humanized form) and semantic aliases (rules that
    # are equivalent on the classifier's native context).
    target_aliases: set[str] = set()
    if target_classifier is not None:
        target_aliases.add(target_classifier.description)
        try:
            target_aliases.add(humanize_description(target_classifier))
        except Exception:
            pass
        # Semantic aliases: rules equivalent to the target IN CONTEXT.
        # E.g. on pure-alpha, "no vowels" ≡ "all letters are consonants".
        target_aliases.update(
            SEMANTIC_ALIASES.get(target_classifier.description, set())
        )

    matches = []
    for desc, fn in SALIENT_ALTERNATIVES:
        if desc in target_aliases:
            continue
        try:
            preds = [fn(item) for item in all_items]
        except Exception:
            continue
        if preds == true_labels or preds == flipped:
            matches.append(desc)

    # Length-based ambiguity: if the set of positive lengths is disjoint
    # from the set of negative lengths, "length == K" or "length > K" is
    # a simpler perfect-separating rule. Skip this check if the target
    # rule IS about length (in which case the disjoint-ness is expected).
    target_uses_length = (
        target_classifier is not None
        and "length" in target_classifier.feature_names
    )
    if not target_uses_length:
        pos_lens = {len(p) for p in positives}
        neg_lens = {len(n) for n in negatives}
        if pos_lens.isdisjoint(neg_lens):
            matches.append(
                f"length is in {sorted(pos_lens)} vs {sorted(neg_lens)}"
            )

    return matches


# ============================================================================
# POSITIVE-SET VALUE DIVERSITY
# ============================================================================

def _is_point_predicate(classifier: Classifier) -> bool:
    """Is this classifier's predicate 'f(x) == k' for a single k?"""
    desc = classifier.description
    return " equals " in desc or " is exactly " in desc


def predicate_has_diverse_support(
    classifier: Classifier,
    item_space: ItemSpace,
    n_samples: int = 300,
    min_nonzero_distinct: int = 2,
) -> bool:
    """
    For int-valued classifiers with non-point predicates, verify that the
    predicate is satisfied by >=2 distinct NON-ZERO feature values in the
    context's sample distribution.

    Catches cases like "count_vowels is a multiple of 3" on 5-letter strings,
    where satisfying values are only {0, 3} — the rule effectively collapses
    to "= 0 or = 3" and a simpler "= 3" rule often works just as well.

    Sparse predicates (perfect square, fibonacci) need >=3 values, since
    two-point sets like {1, 4} feel like an enumerated list rather than
    a rule to the solver.
    """
    if _is_point_predicate(classifier):
        return True

    desc = classifier.description
    # Sparse-value predicates need more values to look like a real rule.
    if "perfect square" in desc or "fibonacci" in desc:
        min_nonzero_distinct = max(min_nonzero_distinct, 3)

    int_features = [
        f for f in classifier.feature_names
        if f in FEATURE_REGISTRY and FEATURE_REGISTRY[f][1] == int
    ]
    if len(int_features) != 1:
        return True

    fname = int_features[0]
    fn = FEATURE_REGISTRY[fname][0]

    satisfying_values: set = set()
    for _ in range(n_samples):
        item = item_space.sample()
        try:
            v = fn(item)
            if v is None:
                continue
            if classifier.classify(item):
                satisfying_values.add(v)
        except Exception:
            continue

    nonzero = {v for v in satisfying_values if v != 0}
    return len(nonzero) >= min_nonzero_distinct


# Features whose value in random strings OFTEN equals the string's length,
# making any rule about them indistinguishable from the same rule about
# length (unless positives include "non-degenerate" items).
LENGTH_COLLAPSIBLE_FEATURES = {
    "num_runs",           # = length iff no adjacent repeats
    "count_unique",       # = length iff all chars distinct
    "shortest_period",    # = length iff no internal repetition
    "count_alpha",        # = length iff all chars are letters (pure alpha)
    "count_digits",       # = length iff all chars are digits (pure digit)
    "count_consonants",   # = length iff all chars are consonants
    "count_vowels",       # = length iff all chars are vowels (rare)
    "count_lowercase",    # = length iff all lowercase (on mixed context)
    "count_uppercase",    # = length iff all uppercase (on mixed context)
}


def positives_not_length_trivial(
    positives: list[str],
    classifier: Classifier,
    min_nondegenerate: int = 1,
) -> bool:
    """
    For features that can coincidentally equal length in random strings,
    require at least `min_nondegenerate` positives where feature < length.

    Example: "num_runs is multiple of 3" on strings with no adjacent repeats
    has num_runs = length. Solver sees rule as "length is multiple of 3".
    Requiring ≥1 positive with num_runs < length (i.e., has adjacent repeats)
    disambiguates.
    """
    int_features = [
        f for f in classifier.feature_names
        if f in FEATURE_REGISTRY and FEATURE_REGISTRY[f][1] == int
           and f in LENGTH_COLLAPSIBLE_FEATURES
    ]
    if not int_features:
        return True

    fname = int_features[0]
    fn = FEATURE_REGISTRY[fname][0]
    nondegenerate = 0
    for p in positives:
        try:
            v = fn(p)
            if v is not None and v < len(p):
                nondegenerate += 1
        except Exception:
            pass
    return nondegenerate >= min_nondegenerate


def positives_span_multiple_values(
    positives: list[str],
    classifier: Classifier,
    min_distinct: int = 2,
    allow_zero_only_if_nonzero_exists: bool = True,
) -> bool:
    """
    For int-valued classifiers with non-point predicates (e.g. "is odd",
    "multiple of 3"), ensure positives span at least `min_distinct` feature
    values.

    Sparse predicates (perfect square, fibonacci) require 3+ values so the
    rule doesn't look like an enumerated list (e.g. "1 or 4") rather than
    a general pattern.

    This prevents problems where all positives have the same feature value —
    which lets the solver explain them with a simpler "= that value" rule.
    """
    desc = classifier.description
    if "perfect square" in desc or "fibonacci" in desc:
        min_distinct = max(min_distinct, 3)

    if _is_point_predicate(classifier):
        return True

    # Only check for single-feature int classifiers
    int_features = [
        f for f in classifier.feature_names
        if f in FEATURE_REGISTRY and FEATURE_REGISTRY[f][1] == int
    ]
    if len(int_features) != 1:
        return True

    fname = int_features[0]
    fn = FEATURE_REGISTRY[fname][0]
    values = set()
    for p in positives:
        try:
            v = fn(p)
            if v is not None:
                values.add(v)
        except Exception:
            pass

    if len(values) < min_distinct:
        return False

    # If 0 is one of the values AND it's the only non-trivial one, reject —
    # positives that are all "absent" and one that's not is weird.
    if allow_zero_only_if_nonzero_exists and 0 in values and len(values) == 1:
        return False

    return True


# ============================================================================
# ITEM DIVERSITY CHECKS
# ============================================================================

def items_are_diverse(items: list[str], min_unique_lengths: int = 2) -> bool:
    """Check that items aren't all identical-looking."""
    if len(set(items)) < len(items):
        return False  # no duplicates allowed
    lengths = set(len(item) for item in items)
    if len(lengths) < min_unique_lengths:
        # All same length is OK for fixed-length spaces (two_digit, three_digit)
        pass
    return True


# ============================================================================
# PROBLEM GENERATION
# ============================================================================

@dataclass
class NegativeProvenance:
    """Tracks how a negative was generated."""
    negative: str
    source_positive: str | None  # which positive it was derived from (None if random)
    method: str  # "close_targeted", "close_simple", or "random"
    edit_distance: int | None = None  # character-level Levenshtein distance

    @staticmethod
    def char_edit_distance(a: str, b: str) -> int:
        """Compute Levenshtein distance between two strings."""
        if len(a) < len(b):
            return NegativeProvenance.char_edit_distance(b, a)
        if not b:
            return len(a)
        prev = list(range(len(b) + 1))
        for i, ca in enumerate(a):
            curr = [i + 1]
            for j, cb in enumerate(b):
                curr.append(min(
                    prev[j + 1] + 1,  # deletion
                    curr[j] + 1,      # insertion
                    prev[j] + (ca != cb),  # substitution
                ))
            prev = curr
        return prev[-1]


@dataclass
class BongardProblem:
    """A generated Bongard problem instance."""
    positives: list[str]
    negatives: list[str]
    classifier: Classifier
    item_space: str
    base_rate: float = 0.5
    neg_types: dict = field(default_factory=dict)  # how negatives were generated
    neg_provenance: list[NegativeProvenance] = field(default_factory=list)
    # Held-out items used for python-function evaluation
    probes: list = field(default_factory=list)


def compatible_item_spaces(classifier: Classifier) -> list[str]:
    """Return item space names compatible with a classifier's features."""
    needed_cats = set()
    for fname in classifier.feature_names:
        if fname in FEATURE_REGISTRY:
            needed_cats.add(FEATURE_REGISTRY[fname][2])
    compatible = []
    for space_name, supported_cats in SPACE_COMPATIBILITY.items():
        if needed_cats <= supported_cats:
            compatible.append(space_name)
    return compatible


def rank_item_spaces(classifier: Classifier) -> list[tuple[str, int]]:
    """
    Rank compatible item spaces by number of distractor categories.

    Distractor categories = categories the space activates that the classifier
    does NOT use. Fewer distractors → narrower hypothesis space → easier for
    the solver (all else equal).

    Returns list of (space_name, n_distractors) sorted ascending.
    """
    needed_cats = set()
    for fname in classifier.feature_names:
        if fname in FEATURE_REGISTRY:
            needed_cats.add(FEATURE_REGISTRY[fname][2])

    ranked = []
    for space_name, supported_cats in SPACE_COMPATIBILITY.items():
        if needed_cats <= supported_cats:
            n_distractors = len(supported_cats - needed_cats)
            ranked.append((space_name, n_distractors))

    ranked.sort(key=lambda x: x[1])
    return ranked


def select_item_space(classifier: Classifier, breadth: str = "narrow") -> str | None:
    """
    Select an item space for a classifier.

    breadth controls how many distractor categories are tolerated:
      - "narrow": pick from spaces with fewest distractors (ties broken randomly)
      - "medium": pick from spaces in the bottom half of distractor counts
      - "wide":   pick any compatible space (original behavior)

    This is the primary lever for controlling hypothesis-space difficulty
    independently of concept complexity.
    """
    ranked = rank_item_spaces(classifier)
    if not ranked:
        return None

    if breadth == "narrow":
        # Pick among spaces tied for minimum distractors
        min_dist = ranked[0][1]
        candidates = [name for name, d in ranked if d == min_dist]
    elif breadth == "medium":
        # Bottom half of distractor counts
        mid = max(1, len(ranked) // 2)
        candidates = [name for name, _ in ranked[:mid]]
    else:  # "wide"
        candidates = [name for name, _ in ranked]

    return random.choice(candidates)


# For classifiers whose positive examples live in a structurally small
# subset of the context's charset, sample directly from that subset rather
# than rejection-sampling from the full charset.
#
# Two flavors:
#  1. POSITIVE_BIASED_CHARSETS — "all chars from this subset" rules
#  2. POSITIVE_GENERATORS — constructive samplers for structural rules
#     (palindromes, sorted, starts_with_X, etc.)

POSITIVE_BIASED_CHARSETS = {
    "all letters vowels": "aeiou",
    "all letters consonants": "bcdfghjklmnpqrstvwxyz",
    "all digits even": "02468",
    "all digits odd": "13579",
}


# Classifiers where close-negatives create spurious "count = 1" / "count is
# odd" ambiguities because the minimal break from the positive structure is
# a single off-type element. Use random negatives instead.
FORCE_RANDOM_NEGATIVES = {
    "all letters vowels", "all letters consonants",
    "all digits even", "all digits odd",
    "digits all same parity", "digits alternating parity",
    "all chars same", "all chars unique",
}


def _choose_len(item_space: ItemSpace) -> int:
    if item_space.min_len == item_space.max_len:
        return item_space.min_len
    return random.randint(item_space.min_len, item_space.max_len)


# Each generator takes an ItemSpace and returns a string that satisfies
# the rule by construction (or None if it can't on this space).
POSITIVE_GENERATORS = {
    # --- Positional constraints — bias a single character ---
    "starts with vowel": lambda sp: (
        random.choice("aeiou") + "".join(random.choice(sp.charset)
                                          for _ in range(_choose_len(sp) - 1))
    ),
    "ends with vowel": lambda sp: (
        "".join(random.choice(sp.charset) for _ in range(_choose_len(sp) - 1))
        + random.choice("aeiou")
    ),
    "starts with digit": lambda sp: (
        random.choice("0123456789")
        + "".join(random.choice(sp.charset) for _ in range(_choose_len(sp) - 1))
    ),
    "ends with digit": lambda sp: (
        "".join(random.choice(sp.charset) for _ in range(_choose_len(sp) - 1))
        + random.choice("0123456789")
    ),

    # --- Structural — build with the property by construction ---
    "is palindrome": lambda sp: (
        lambda s: (s + s[::-1][1:] if _choose_len(sp) % 2 else s + s[::-1])
    )("".join(random.choice(sp.charset) for _ in range(_choose_len(sp) // 2 + (_choose_len(sp) % 2)))),
    "all chars same": lambda sp: random.choice(sp.charset) * _choose_len(sp),
    "all chars unique": lambda sp: "".join(
        random.sample(sp.charset, _choose_len(sp))
    ),
    "first equals last": lambda sp: (
        lambda c, mid: c + mid + c
    )(random.choice(sp.charset),
      "".join(random.choice(sp.charset) for _ in range(max(0, _choose_len(sp) - 2)))),

    # --- Sort-based: sample then sort ---
    "letters sorted ascending": lambda sp: "".join(sorted(
        random.choice(sp.charset) for _ in range(_choose_len(sp))
    )),
    "letters sorted descending": lambda sp: "".join(sorted(
        (random.choice(sp.charset) for _ in range(_choose_len(sp))), reverse=True
    )),
    "digits sorted ascending": lambda sp: "".join(sorted(
        random.choice("0123456789") for _ in range(_choose_len(sp))
    )),
    "digits sorted descending": lambda sp: "".join(sorted(
        (random.choice("0123456789") for _ in range(_choose_len(sp))), reverse=True
    )),

    # --- Parity-structured ---
    "digits all same parity": lambda sp: "".join(
        random.choice(random.choice(["02468", "13579"])) for _ in range(_choose_len(sp))
    ),
    "digits alternating parity": lambda sp: "".join(
        random.choice("02468") if (i + random.randint(0, 1)) % 2 == 0 else random.choice("13579")
        for i in range(_choose_len(sp))
    ),
}


def generate_items(
    classifier: Classifier,
    item_space: ItemSpace,
    n_pos: int = 6,
    n_neg: int = 6,
    max_sampling_attempts: int = 5000,
) -> tuple[list[str], list[str], dict] | None:
    """
    Generate positive and negative items.

    Strategy:
      1. Random sampling to build a pool of positives.
      2. For each positive, generate a close negative via targeted perturbation.
         For AND-classifiers, break exactly one conjunct per negative.
      3. If we can't get enough close negatives, supplement with random negatives.

    Returns (positives, negatives, neg_type_counts) or None.
    """
    # Phase 1: Collect positives. If the classifier has a concept-aware
    # positive generator (either a biased charset or a constructive sampler),
    # use it. Otherwise do uniform rejection sampling.
    positive_biased_charset = POSITIVE_BIASED_CHARSETS.get(classifier.description)
    positive_generator = POSITIVE_GENERATORS.get(classifier.description)
    # Three sets:
    #  - `sampled`: every item we've tried (for sample-level dedup)
    #  - `positive_set`: items placed in positives (must not reappear as negatives)
    #  - `used_as_negative`: items placed as negatives in the final output so far
    # Phase 2/3 dedup against `positive_set | used_as_negative`, NOT against
    # the candidate `random_negatives` pool (those are candidates, not placed).
    positives = []
    random_negatives = []
    sampled: set[str] = set()
    positive_set: set[str] = set()

    # If we have a positive generator (biased charset or constructive
    # sampler), use it to fill positives fast.
    if positive_biased_charset or positive_generator:
        if item_space.min_len == item_space.max_len:
            lengths = [item_space.min_len]
        else:
            lengths = list(range(item_space.min_len, item_space.max_len + 1))
        for _ in range(max_sampling_attempts):
            try:
                if positive_generator is not None:
                    item = positive_generator(item_space)
                else:
                    n = random.choice(lengths)
                    item = "".join(
                        random.choice(positive_biased_charset) for _ in range(n)
                    )
            except Exception:
                continue
            if item in sampled:
                continue
            sampled.add(item)
            try:
                if classifier.classify(item):  # by construction, usually True
                    if len(positives) < n_pos * 4:
                        positives.append(item)
                        positive_set.add(item)
            except Exception:
                continue
            if len(positives) >= n_pos * 4:
                break

    # Standard uniform sampling (fills negatives AND positives if no bias hit).
    for _ in range(max_sampling_attempts):
        item = item_space.sample()
        if item in sampled:
            continue
        sampled.add(item)
        try:
            result = classifier.classify(item)
        except Exception:
            continue
        if result and len(positives) < n_pos * 4:
            positives.append(item)
            positive_set.add(item)
        elif not result and len(random_negatives) < n_neg * 4:
            random_negatives.append(item)
        # Early exit once we have enough of both
        if len(positives) >= n_pos * 4 and len(random_negatives) >= n_neg * 4:
            break

    if len(positives) < n_pos:
        return None  # can't generate enough positives

    # `seen` is the dedup set for Phase 2/3: items already committed to
    # positives or to the final negatives list. random_negatives themselves
    # are candidates, not yet committed, so they're NOT in `seen`.
    seen = set(positive_set)

    # Select diverse positives — feature-value aware for int classifiers
    # with non-point predicates (so "count_vowels is odd" gets counts 1,3,5
    # instead of all-5).
    positives = _select_feature_diverse(positives, n_pos, classifier)
    # Also apply feature-diverse selection to the negative pool. We build
    # the full pool of random negatives but don't use them directly for
    # close-negatives — this just helps downstream checks.
    random_negatives = _select_feature_diverse(
        random_negatives, n_neg * 4, classifier
    )

    # Phase 2: Generate close negatives.
    # Close-negatives (minimal perturbations) are highly informative for
    # STRUCTURAL rules (palindrome, starts_with_X, sorted, etc.) — a single
    # char changed, rule broken, solver sees the cue clearly.
    # But they fail for HOMOGENEITY rules (all vowels, all even, etc.):
    # the minimal break is always "exactly 1 non-matching item", which
    # makes "count_non_matching = 1" or "count_non_matching is odd"
    # perfectly separate the sample. Those specific classifiers go into
    # FORCE_RANDOM_NEGATIVES.
    skip_close_negatives = classifier.description in FORCE_RANDOM_NEGATIVES

    close_negatives = []
    provenance = []  # track how each negative was generated
    neg_types = {"close_targeted": 0, "close_simple": 0, "random": 0}

    # Detect AND-classifiers and extract conjuncts
    conjuncts = _extract_conjuncts(classifier)

    if not skip_close_negatives:
        for pos_item in positives:
            neg = None
            method = None

            if conjuncts and len(conjuncts) >= 2:
                # AND-classifier: break exactly one conjunct
                result = generate_close_negative_for_and(
                    pos_item, conjuncts, item_space
                )
                if result is not None:
                    neg, _ = result
                    method = "close_targeted"
                    neg_types["close_targeted"] += 1

            if neg is None:
                # Fallback: generic close negative
                neg = generate_close_negative_simple(pos_item, classifier, item_space)
                if neg is not None:
                    method = "close_simple"
                    neg_types["close_simple"] += 1

            if neg is not None and neg not in seen:
                close_negatives.append(neg)
                seen.add(neg)
                ed = NegativeProvenance.char_edit_distance(pos_item, neg)
                provenance.append(NegativeProvenance(
                    negative=neg, source_positive=pos_item,
                    method=method, edit_distance=ed,
                ))

    # Phase 3: Fill remaining slots with random negatives
    negatives = close_negatives[:n_neg]
    provenance = provenance[:n_neg]
    if len(negatives) < n_neg:
        for rn in random_negatives:
            if rn not in seen and len(negatives) < n_neg:
                negatives.append(rn)
                seen.add(rn)
                neg_types["random"] += 1
                provenance.append(NegativeProvenance(
                    negative=rn, source_positive=None,
                    method="random", edit_distance=None,
                ))

    if len(negatives) < n_neg:
        return None

    return positives, negatives[:n_neg], neg_types, provenance[:n_neg]


def _select_feature_diverse(
    items: list[str], n: int, classifier: Classifier,
) -> list[str]:
    """
    Select n items, maximizing diversity on the classifier's primary int feature.
    For non-point predicates, this spreads positives across multiple values
    of the feature — e.g., a "count_vowels is odd" positive set gets a mix
    of 1, 3, 5 rather than all-5.
    Falls back to length-based diversity if the classifier has no single
    int feature.
    """
    if len(items) <= n:
        return items

    int_features = [
        f for f in classifier.feature_names
        if f in FEATURE_REGISTRY and FEATURE_REGISTRY[f][1] == int
    ]

    # For single-int-feature non-point classifiers: bucket by feature value
    if len(int_features) == 1 and not _is_point_predicate(classifier):
        fname = int_features[0]
        fn = FEATURE_REGISTRY[fname][0]
        by_value: dict = {}
        for item in items:
            try:
                v = fn(item)
            except Exception:
                v = None
            by_value.setdefault(v, []).append(item)

        # Round-robin across feature values, preferring values that have items
        values = [v for v in by_value if v is not None]
        random.shuffle(values)
        selected = []
        idx = 0
        while len(selected) < n and any(by_value[v] for v in values):
            v = values[idx % len(values)]
            if by_value[v]:
                selected.append(by_value[v].pop())
            idx += 1
            if idx > n * 20:
                break

        if len(selected) >= n:
            return selected[:n]
        # Fill any shortfall with leftover items
        remaining = [x for v in by_value.values() for x in v]
        selected.extend(remaining[:n - len(selected)])
        return selected[:n]

    # Fallback: length-based diversity
    return _select_diverse(items, n)


def _select_diverse(items: list[str], n: int) -> list[str]:
    """Select n items maximizing diversity (varied lengths, varied chars)."""
    if len(items) <= n:
        return items

    # Group by length, pick from each group
    by_length = {}
    for item in items:
        by_length.setdefault(len(item), []).append(item)

    selected = []
    # Round-robin across lengths
    lengths = sorted(by_length.keys())
    idx = 0
    while len(selected) < n:
        length = lengths[idx % len(lengths)]
        if by_length[length]:
            selected.append(by_length[length].pop(0))
        idx += 1
        if idx > n * 10:
            break

    # If still short, fill from remaining
    if len(selected) < n:
        remaining = [item for group in by_length.values() for item in group]
        selected.extend(remaining[:n - len(selected)])

    return selected[:n]


def _extract_conjuncts(classifier: Classifier) -> list[Callable] | None:
    """
    For AND-classifiers (depth=1 with two bool feature names),
    extract the individual conjunct functions.
    Returns list of callable conjuncts, or None if not an AND-classifier.
    """
    if classifier.depth != 1:
        return None
    if " AND " not in classifier.description:
        return None

    # Extract individual feature functions from the registry
    conjuncts = []
    for fname in classifier.feature_names:
        if fname in FEATURE_REGISTRY:
            fn = FEATURE_REGISTRY[fname][0]
            rtype = FEATURE_REGISTRY[fname][1]
            if rtype == bool:
                conjuncts.append(fn)

    return conjuncts if len(conjuncts) >= 2 else None


def generate_bongard_problem(
    classifier: Classifier,
    item_space_name: str | None = None,
    space_breadth: str = "narrow",
    all_classifiers: list[Classifier] | None = None,
    n_pos: int = 6,
    n_neg: int = 6,
    max_retries: int = 30,
) -> BongardProblem | None:
    """
    Generate a complete Bongard problem for a given classifier.

    space_breadth: "narrow", "medium", or "wide" — controls how many
    distractor feature categories the item space activates beyond what
    the classifier needs. Narrower = smaller hypothesis space for the solver.
    """
    if item_space_name is None:
        item_space_name = select_item_space(classifier, breadth=space_breadth)
        if item_space_name is None:
            return None

    item_space = ITEM_SPACES.get(item_space_name)
    if item_space is None:
        return None

    for attempt in range(max_retries):
        result = generate_items(classifier, item_space, n_pos, n_neg)
        if result is None:
            continue

        positives, negatives, neg_types, provenance = result

        # Diversity check
        if not items_are_diverse(positives + negatives):
            continue

        # Ambiguity check: no simpler classifier also perfectly separates
        if all_classifiers:
            ambiguous = check_ambiguity(
                positives, negatives, classifier, all_classifiers,
                item_space=item_space,
            )
            if ambiguous:
                continue

        random.shuffle(positives)
        random.shuffle(negatives)

        base_rate = getattr(classifier, "_base_rate", 0.5)

        return BongardProblem(
            positives=positives,
            negatives=negatives,
            classifier=classifier,
            item_space=item_space_name,
            base_rate=base_rate,
            neg_types=neg_types,
            neg_provenance=provenance,
        )

    return None


# ============================================================================
# HUMAN-READABLE DESCRIPTIONS
# ============================================================================

# Map feature names to natural phrasing.
# Format: feature_name → (noun_phrase, verb_for_predicate_context)
# e.g. "count_vowels" → ("the number of vowels", "is")
FEATURE_LABELS = {
    # Length/count
    "length": ("the length of the string", "is"),
    "count_alpha": ("the number of letters", "is"),
    "count_digits": ("the number of digits", "is"),
    "count_symbols": ("the number of symbols", "is"),
    "count_uppercase": ("the number of uppercase letters", "is"),
    "count_lowercase": ("the number of lowercase letters", "is"),
    "count_vowels": ("the number of vowels", "is"),
    "count_consonants": ("the number of consonants", "is"),
    "count_unique": ("the number of distinct characters", "is"),
    "max_run_length": ("the longest run of repeated characters", "is"),
    "num_runs": ("the number of character runs", "is"),
    "count_char_type_transitions": ("the number of character-type transitions", "is"),

    # Character identity
    "first_char_type": ("the type of the first character", "is"),
    "last_char_type": ("the type of the last character", "is"),

    # Structure (bool)
    "is_palindrome": "the string is a palindrome",
    "all_chars_same": "all characters are the same",
    "all_chars_unique": "all characters are distinct",
    "first_equals_last": "the first and last characters are the same",
    "starts_with_vowel": "the string starts with a vowel",
    "ends_with_vowel": "the string ends with a vowel",
    "starts_with_digit": "the string starts with a digit",
    "ends_with_digit": "the string ends with a digit",
    "has_digits": "the string contains digits",
    "has_letters": "the string contains letters",
    "has_symbols": "the string contains symbols",
    "has_repeated_char": "the string contains a repeated character",
    "is_alternating_case": "the string alternates between upper and lowercase",
    "is_alternating_char_type": "the string alternates between character types",

    # Alphabetic
    "alpha_sum": ("the sum of letter values (a=1, b=2, ...)", "is"),
    "alpha_product": ("the product of letter values", "is"),
    "letters_sorted_ascending": "the letters are in alphabetical order",
    "letters_sorted_descending": "the letters are in reverse alphabetical order",
    "letters_contiguous": "the letters are consecutive in the alphabet",
    "alpha_diffs_constant": "consecutive letters have equal spacing",
    "alpha_diffs_positive": "each letter comes later in the alphabet than the previous",
    "alpha_step_size": ("the step size between consecutive letters", "is"),

    # Digit
    "digit_sum": ("the sum of the digits", "is"),
    "digit_product": ("the product of the digits", "is"),
    "num_even_digits": ("the number of even digits", "is"),
    "num_odd_digits": ("the number of odd digits", "is"),
    "max_digit": ("the largest digit", "is"),
    "min_digit": ("the smallest digit", "is"),
    "digit_range": ("the range between largest and smallest digits", "is"),
    "digits_sorted_ascending": "the digits are in ascending order",
    "digits_sorted_descending": "the digits are in descending order",
    "digit_diffs_constant": "consecutive digits have equal spacing",
    "digit_diffs_increasing": "the gaps between consecutive digits are increasing",
    "digits_all_same_parity": "all digits have the same parity (all even or all odd)",
    "digits_alternating_parity": "the digits alternate between even and odd",
    "all_digits_even": "all digits are even",
    "all_digits_odd": "all digits are odd",
    "all_letters_vowels": "all letters are vowels",
    "all_letters_consonants": "all letters are consonants",
    "opposite_parity_digits": "the digits have opposite parity",

    # Numeric
    "numeric_value": ("the numeric value", "is"),
    "is_prime_number": "the number is prime",
    "is_perfect_square": "the number is a perfect square",
    "is_power_of_2": "the number is a power of 2",
    "is_fibonacci": "the number is a Fibonacci number",
    "num_divisors": ("the number of divisors", "is"),
    "is_palindrome_number": "the number reads the same forwards and backwards",
    "binary_digit_count": ("the number of 1s in binary", "is"),
    "binary_length": ("the length in binary", "is"),

    # Encoding
    "ascii_sum": ("the sum of ASCII values", "is"),
    "scrabble_score": ("the Scrabble score", "is"),
    "morse_length": ("the total Morse code length", "is"),
    "roman_numeral_length": ("the Roman numeral length", "is"),
    "count_enclosed_regions": ("the number of enclosed regions in the characters", "is"),
    "count_curved_letters": ("the number of curved letters", "is"),
    "count_ascenders": ("the number of letters with ascenders", "is"),
    "count_descenders": ("the number of letters with descenders", "is"),
    "count_symmetric_chars": ("the number of visually symmetric characters", "is"),

    # Pattern
    "cv_pattern_is_palindrome": "the consonant-vowel pattern is a palindrome",
    "is_periodic": "the string has a repeating pattern",
    "shortest_period": ("the shortest repeating period", "is"),
    "has_repeated_bigram": "the string contains a repeated two-character sequence",

    # Ratio
    "vowel_ratio": ("the vowel ratio", "is"),
    "uppercase_ratio": ("the uppercase ratio", "is"),
    "unique_ratio": ("the ratio of distinct characters", "is"),
}


def _humanize_predicate(feature_name: str, predicate_str: str) -> str:
    """
    Convert a mechanical classifier description into natural language.

    Examples:
        "count vowels is even"  → "the number of vowels is even"
        "min digit divisible by 3" → "the smallest digit is divisible by 3"
        "alpha sum > 5" → "the sum of letter values (a=1, b=2, ...) is greater than 5"
        "is palindrome" → "the string is a palindrome"
    """
    label_info = FEATURE_LABELS.get(feature_name)
    if label_info is None:
        return predicate_str  # fallback to mechanical description

    # Bool features: label_info is a string
    if isinstance(label_info, str):
        return label_info

    # Int features: label_info is (noun_phrase, verb)
    noun, verb = label_info

    # Parse the predicate part (everything after the feature name in the description)
    feat_hr = feature_name.replace("_", " ")
    pred_part = predicate_str
    if pred_part.startswith(feat_hr):
        pred_part = pred_part[len(feat_hr):].strip()

    # Rewrite common predicate patterns
    if pred_part == "is even":
        return f"{noun} is even"
    elif pred_part == "is odd":
        return f"{noun} is odd"
    elif pred_part == "is prime":
        return f"{noun} is prime"
    elif pred_part == "is a perfect square":
        return f"{noun} is a perfect square"
    elif pred_part.startswith("divisible by "):
        k = pred_part[len("divisible by "):]
        return f"{noun} is a multiple of {k}"
    elif pred_part.startswith("> "):
        k = pred_part[2:]
        return f"{noun} is greater than {k}"
    elif pred_part.startswith(">= "):
        k = pred_part[3:]
        return f"{noun} is at least {k}"
    elif pred_part.startswith("equals "):
        k = pred_part[len("equals "):]
        return f"{noun} {verb} exactly {k}"
    else:
        return f"{noun} {pred_part}"


def humanize_description(classifier: Classifier) -> str:
    """
    Convert a classifier's mechanical description to natural language.
    Handles depth-0 (single feature), AND compositions, and comparisons.
    """
    desc = classifier.description

    # --- Depth 0: single feature ---
    if classifier.depth == 0 and len(classifier.feature_names) == 1:
        return _humanize_predicate(classifier.feature_names[0], desc)

    # --- AND composition ---
    if " AND " in desc:
        parts = desc.split(" AND ", 1)
        fnames = classifier.feature_names

        if len(fnames) >= 2:
            left = _humanize_predicate(fnames[0], parts[0].strip())
            right = _humanize_predicate(fnames[1], parts[1].strip())
            return f"{left}, AND {right}"
        else:
            return desc

    # --- Comparison (f1 op f2) ---
    for op_sym, op_word in [(">=", "at least"), ("==", "equal to"), (">", "greater than")]:
        if f" {op_sym} " in desc:
            parts = desc.split(f" {op_sym} ", 1)
            if len(classifier.feature_names) >= 2:
                left_info = FEATURE_LABELS.get(classifier.feature_names[0])
                right_info = FEATURE_LABELS.get(classifier.feature_names[1])
                if left_info and right_info:
                    ln = left_info[0] if isinstance(left_info, tuple) else left_info
                    rn = right_info[0] if isinstance(right_info, tuple) else right_info
                    return f"{ln} is {op_word} {rn}"
            return desc

    return desc


# ============================================================================
# CLASSIFICATION PROBES
# ============================================================================

@dataclass
class ProbeItem:
    """A held-out item for evaluating whether the solver found the rule."""
    item: str
    label: bool  # True = Group A, False = Group B
    label_str: str  # "A" or "B"


def generate_probes(
    problem: BongardProblem,
    n_probes: int = 4,
    max_attempts: int = 2000,
    item_space: ItemSpace | None = None,
) -> list[ProbeItem]:
    """
    Generate held-out items for classification probes.

    Returns a balanced set of new items (not in the original problem)
    that the solver must classify as Group A or Group B.
    The ground truth is computed from the classifier.

    Good probes are items that are NOT trivially classifiable — they should
    be "in the neighborhood" of the decision boundary (close to items in
    both groups).

    `item_space` can be passed directly when the context is known (preferred).
    Falls back to looking up by problem.item_space name (works only for the
    legacy fixed set of item spaces in ITEM_SPACES, not per-problem contexts).
    """
    if item_space is None:
        item_space = ITEM_SPACES.get(problem.item_space)
    if item_space is None:
        return []

    existing = set(problem.positives + problem.negatives)
    classifier = problem.classifier

    # Collect candidate probes
    pos_probes = []
    neg_probes = []

    for _ in range(max_attempts):
        item = item_space.sample()
        if item in existing:
            continue
        try:
            label = classifier.classify(item)
        except Exception:
            continue

        if label and len(pos_probes) < n_probes:
            pos_probes.append(item)
            existing.add(item)
        elif not label and len(neg_probes) < n_probes:
            neg_probes.append(item)
            existing.add(item)

        if len(pos_probes) >= n_probes and len(neg_probes) >= n_probes:
            break

    # Build balanced probe set: equal positive and negative
    n_each = min(len(pos_probes), len(neg_probes), n_probes // 2 or 1)
    probes = []
    for item in pos_probes[:n_each]:
        probes.append(ProbeItem(item=item, label=True, label_str="A"))
    for item in neg_probes[:n_each]:
        probes.append(ProbeItem(item=item, label=False, label_str="B"))

    random.shuffle(probes)
    return probes


def format_probe_prompt(problem: BongardProblem, probe: ProbeItem) -> str:
    """
    Format a single classification probe.

    The solver sees the Bongard problem, then is asked to classify a new item.
    """
    lines = []
    lines.append("Here is a Bongard problem. Items in Group A all share a property")
    lines.append("that items in Group B do not have.\n")
    lines.append("Group A (positive examples):")
    for i, item in enumerate(problem.positives, 1):
        lines.append(f"  {i}. {item}")
    lines.append("")
    lines.append("Group B (negative examples):")
    for i, item in enumerate(problem.negatives, 1):
        lines.append(f"  {i}. {item}")
    lines.append("")
    lines.append(f"New item: {probe.item}")
    lines.append("")
    lines.append("Does this new item belong to Group A or Group B? Answer with just the letter.")
    return "\n".join(lines)


# ============================================================================
# FORMATTING
# ============================================================================

def format_problem_for_prompt(problem: BongardProblem) -> str:
    """Format a Bongard problem as a text prompt for an LLM (open-ended)."""
    lines = []
    lines.append("Here is a Bongard problem. Items in Group A all share a property")
    lines.append("that items in Group B do not have. What is the rule?\n")
    lines.append("Group A (positive examples):")
    for i, item in enumerate(problem.positives, 1):
        lines.append(f"  {i}. {item}")
    lines.append("")
    lines.append("Group B (negative examples):")
    for i, item in enumerate(problem.negatives, 1):
        lines.append(f"  {i}. {item}")
    return "\n".join(lines)


def format_problem_as_csv_row(
    problem: BongardProblem,
    problem_id: int,
    n_probes: int = 10,
) -> dict:
    """Format a problem as a CSV-compatible dict.

    Primary columns (consumed by Inspect AI's record_to_sample):
      - problem : the open-ended prompt text
      - solution: the humanized rule description (for LLM-judge scoring)

    Additional metadata columns (for python-function scoring + analysis):
      - probes  : held-out items with ground-truth labels (JSON list)
      - positives, negatives, item_space, features, base_rate, etc.
    """
    edit_dists = [p.edit_distance for p in problem.neg_provenance
                  if p.edit_distance is not None]
    ed_stats = {}
    if edit_dists:
        ed_stats = {
            "min": min(edit_dists),
            "max": max(edit_dists),
            "mean": round(sum(edit_dists) / len(edit_dists), 2),
        }

    prov_data = [
        {
            "neg": p.negative,
            "src": p.source_positive,
            "method": p.method,
            "ed": p.edit_distance,
        }
        for p in problem.neg_provenance
    ]

    answer_human = humanize_description(problem.classifier)

    # Probes were generated at problem creation (item_space still in scope).
    probe_data = [{"item": p.item, "label": p.label_str} for p in problem.probes]

    return {
        "problem": format_problem_for_prompt(problem),
        "solution": answer_human,
        "probes": json.dumps(probe_data),
        "positives": json.dumps(problem.positives),
        "negatives": json.dumps(problem.negatives),
        "item_space": problem.item_space,
        "answer_raw": problem.classifier.description,
        "features": json.dumps(problem.classifier.feature_names),
        "depth": problem.classifier.depth,
        "base_rate": f"{problem.base_rate:.2f}",
        "neg_types": json.dumps(problem.neg_types),
        "edit_distance_stats": json.dumps(ed_stats),
        "neg_provenance": json.dumps(prov_data),
    }


# ============================================================================
# DATASET GENERATION
# ============================================================================

def generate_dataset(
    n_problems: int = 50,
    difficulty_filter: str | None = None,
    max_depth: int = 0,
    space_breadth: str = "narrow",
    seed: int | None = None,
    min_base_rate: float = 0.02,
    max_base_rate: float = 0.85,
) -> list[BongardProblem]:
    """
    Generate a dataset of Bongard problems.

    space_breadth: "narrow", "medium", or "wide" — controls how many
    distractor feature categories are activated by the item space.
    This is the primary lever for hypothesis-space difficulty:
      - "narrow": items only activate categories the classifier uses
                  (solver can prune most features from the items alone)
      - "medium": some distractor categories present
      - "wide":   any compatible space (original behavior, hardest)
    """
    if seed is not None:
        random.seed(seed)

    # Build full classifier library
    all_classifiers = build_classifier_library()
    print(f"Built classifier library: {len(all_classifiers)} classifiers")

    # Apply depth filter early
    if max_depth is not None:
        all_classifiers_dep = [c for c in all_classifiers if c.depth <= max_depth]
        print(f"After depth filter (max_depth={max_depth}): {len(all_classifiers_dep)}")
    else:
        all_classifiers_dep = all_classifiers

    # For each classifier, build its per-problem context via axis propagation,
    # then check that the classifier's base rate in that context is viable.
    viable: list[tuple[Classifier, ConceptContext]] = []

    for clf in all_classifiers_dep:
        ctx = build_context(clf, empirical_check=True, min_each_label=8)
        if ctx is None:
            continue
        item_space = ctx.as_item_space()
        # Larger sample for stable low-end estimates (at 3% true rate, n=500
        # gives expected ~15 positives; much more reliable than n=150).
        rate = compute_base_rate(clf, item_space, n_samples=500)
        # For classifiers with a concept-aware positive generator, ignore
        # the lower base-rate bound: uniform sampling may give ~0% but we
        # can still construct positives directly. The upper bound still
        # applies (informativeness).
        has_gen = (
            clf.description in POSITIVE_GENERATORS
            or clf.description in POSITIVE_BIASED_CHARSETS
        )
        if has_gen:
            if rate is None or rate > max_base_rate:
                continue
        else:
            if rate is None or not (min_base_rate <= rate <= max_base_rate):
                continue
        # Reject classifiers whose predicate collapses to <2 non-zero values
        # in this context (e.g. "multiple of 3 vowels" on L5 → only 0,3).
        if not predicate_has_diverse_support(clf, item_space):
            continue
        clf._base_rate = rate
        viable.append((clf, ctx))

    print(f"Viable (classifier, context) pairs: {len(viable)}")

    # Apply difficulty filter
    if difficulty_filter:
        viable = [(c, ctx) for c, ctx in viable
                  if c.difficulty_hint == difficulty_filter]
        print(f"After difficulty filter '{difficulty_filter}': {len(viable)}")

    if not viable:
        print("No viable classifiers found!")
        return [], all_classifiers

    # Show distractor stats: how many OTHER features are live in the context
    from_ = FEATURE_REGISTRY
    distractor_counts = []
    for clf, ctx in viable:
        clf_feats = set(clf.feature_names)
        live_count = 0
        for fname in from_:
            reqs = FEATURE_AXES.get(fname, {})
            if _ctx_satisfies(ctx, reqs):
                if fname not in clf_feats:
                    live_count += 1
        distractor_counts.append(live_count)
    if distractor_counts:
        avg = sum(distractor_counts) / len(distractor_counts)
        print(f"Live distractor features: min={min(distractor_counts)} "
              f"max={max(distractor_counts)} mean={avg:.1f}")

    # --- Feature-balanced weighting + per-feature dedup ---
    # Group viable classifiers by their feature key (tuple of feature names).
    # Weight each classifier by 1 / |its group|, so every feature contributes
    # equal total probability mass regardless of how many predicate variants
    # survived. Also enforce per-feature dedup: at most one problem per
    # feature key in the final dataset.
    from collections import Counter
    feature_keys = [tuple(sorted(c.feature_names)) for c, _ in viable]
    group_sizes = Counter(feature_keys)
    weights = [1.0 / group_sizes[k] for k in feature_keys]

    n_unique_features = len(group_sizes)
    print(f"Distinct feature-keys (unique concepts by feature): {n_unique_features}")

    # Compute per-feature cap. Some features only have 1 viable classifier
    # (e.g. `is_palindrome`), so we need slack — allow up to 2x the average
    # so high-variety features can cover for low-variety ones.
    target_avg = n_problems / max(1, n_unique_features)
    max_per_feature = max(1, int(target_avg * 2 + 0.9999))
    if max_per_feature > 1:
        print(f"  Allowing up to {max_per_feature} problems per feature "
              f"to reach n={n_problems}")

    # Generate problems
    problems = []
    attempts = 0
    max_total_attempts = n_problems * 50
    feature_use_count: dict = Counter()
    # Track which specific classifier descriptions are used (still dedup at
    # the classifier level — no duplicate "count_vowels is even" twice).
    used_descriptions: set = set()

    indices = list(range(len(viable)))

    while len(problems) < n_problems and attempts < max_total_attempts:
        attempts += 1

        # Filter out indices whose feature has hit its per-feature cap OR
        # whose specific classifier description has already been used.
        active = [
            (i, w) for i, w in zip(indices, weights)
            if feature_use_count[feature_keys[i]] < max_per_feature
            and viable[i][0].description not in used_descriptions
        ]
        if not active:
            break

        active_idx = [i for i, _ in active]
        active_w = [w for _, w in active]
        i = random.choices(active_idx, weights=active_w, k=1)[0]
        clf, ctx = viable[i]
        fkey = feature_keys[i]

        problem = generate_bongard_problem_ctx(
            classifier=clf,
            context=ctx,
            all_classifiers=all_classifiers,
        )
        if problem is not None:
            problems.append(problem)
            feature_use_count[fkey] += 1
            used_descriptions.add(clf.description)
            if len(problems) % 10 == 0:
                print(f"  Generated {len(problems)}/{n_problems} problems...")
        else:
            # This classifier couldn't produce a valid problem.
            # Mark its feature key as used only if we've truly exhausted
            # other predicates for this feature in this call.
            # (For simplicity: count as used to avoid infinite retries.)
            # Actually: give it a few retries before banning the key.
            pass

    print(f"Generated {len(problems)} problems in {attempts} attempts")
    return problems, all_classifiers


def _ctx_satisfies(ctx: ConceptContext, reqs: dict) -> bool:
    """Check whether a context satisfies a feature's axis requirements."""
    for axis, accepted in reqs.items():
        if axis == "case":
            if ctx.case not in accepted:
                return False
        elif axis == "charset":
            if ctx.charset not in accepted:
                return False
        elif axis == "length_mode":
            if ctx.length_mode not in accepted:
                return False
        elif axis == "has_spaces":
            # We never include spaces; require the feature's axis to allow "no"
            if "no" not in accepted:
                return False
    return True


def generate_bongard_problem_ctx(
    classifier: Classifier,
    context: ConceptContext,
    all_classifiers: list[Classifier] | None = None,
    n_pos: int = 6,
    n_neg: int = 6,
    max_retries: int = 30,
) -> BongardProblem | None:
    """Generate a Bongard problem using a per-classifier context."""
    item_space = context.as_item_space()

    for _ in range(max_retries):
        result = generate_items(classifier, item_space, n_pos, n_neg)
        if result is None:
            continue

        positives, negatives, neg_types, provenance = result

        if not items_are_diverse(positives + negatives):
            continue

        # Positives must span multiple feature values for non-point predicates
        if not positives_span_multiple_values(positives, classifier):
            continue
        if not positives_span_multiple_values(negatives, classifier):
            continue

        # For features that often coincidentally equal length, require at least
        # one positive where feature < length (so the rule doesn't collapse to
        # an equivalent rule about length).
        if not positives_not_length_trivial(positives, classifier):
            continue

        # NEW: reject if any salient simple alternative perfectly separates
        salient_matches = find_salient_ambiguities(positives, negatives,
                                                   target_classifier=classifier)
        if salient_matches:
            continue

        if all_classifiers:
            ambiguous = check_ambiguity(
                positives, negatives, classifier, all_classifiers,
                item_space=item_space,
            )
            if ambiguous:
                continue

        random.shuffle(positives)
        random.shuffle(negatives)

        base_rate = getattr(classifier, "_base_rate", 0.5)

        # Generate held-out probes while the ItemSpace is still in scope
        # (avoids the ITEM_SPACES-dict-only lookup path in generate_probes).
        problem = BongardProblem(
            positives=positives,
            negatives=negatives,
            classifier=classifier,
            item_space=context.name(),
            base_rate=base_rate,
            neg_types=neg_types,
            neg_provenance=provenance,
        )
        problem.probes = generate_probes(problem, n_probes=10, item_space=item_space)
        return problem

    return None


def save_dataset(problems: list[BongardProblem], output_path: str):
    """Save problems to CSV with open-ended prompts and classification probes."""
    if not problems:
        print("No problems to save.")
        return

    rows = [format_problem_as_csv_row(p, i) for i, p in enumerate(problems)]
    fieldnames = list(rows[0].keys())

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Saved {len(problems)} problems to {output_path}")


# ============================================================================
# CLI
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Generate Bongard text problems")
    parser.add_argument("--n", type=int, default=50,
                        help="Number of problems to generate")
    parser.add_argument("--difficulty", type=str, default=None,
                        choices=["easy", "medium", "hard", "very_hard"],
                        help="Filter by difficulty level")
    parser.add_argument("--output", type=str, default="bongard_problems.csv",
                        help="Output CSV path")
    parser.add_argument("--seed", type=int, default=42,
                        help="Random seed")
    parser.add_argument("--show", type=int, default=3,
                        help="Number of example problems to display")
    parser.add_argument("--max-depth", type=int, default=0,
                        help="Maximum classifier depth (0=single concept, 1=AND of two)")
    parser.add_argument("--breadth", type=str, default="narrow",
                        choices=["narrow", "medium", "wide"],
                        help="Item space breadth: narrow=fewest distractor categories, "
                             "wide=any compatible space")
    parser.add_argument("--min-base-rate", type=float, default=0.03,
                        help="Minimum base rate for classifiers (default 0.03 — "
                             "below this, positive sampling gets expensive; "
                             "generate_items has a 5000-sample timeout as backstop)")
    parser.add_argument("--max-base-rate", type=float, default=0.85,
                        help="Maximum base rate (informativeness guard: rules "
                             "true of >85%% of items look indistinguishable "
                             "from random for the solver)")
    args = parser.parse_args()

    problems, all_classifiers = generate_dataset(
        n_problems=args.n,
        difficulty_filter=args.difficulty,
        max_depth=args.max_depth,
        space_breadth=args.breadth,
        seed=args.seed,
        min_base_rate=args.min_base_rate,
        max_base_rate=args.max_base_rate,
    )

    for i, problem in enumerate(problems[:args.show]):
        print(f"\n{'='*60}")
        print(f"Problem {i+1}")
        print(f"  Concept:    {humanize_description(problem.classifier)}")
        print(f"  Raw:        {problem.classifier.description}")
        print(f"  Space:      {problem.item_space}")
        print(f"  Base rate:  {problem.base_rate:.0%}")

        # Show open-ended format
        print(f"{'='*60}")
        print(format_problem_for_prompt(problem))

        # Show classification probes
        probes = generate_probes(problem, n_probes=4)
        if probes:
            print(f"\n--- Classification Probes ---")
            for p in probes:
                print(f"  {p.item}  → {p.label_str}")
            print(f"\n  Example probe prompt:")
            print(f"  {format_probe_prompt(problem, probes[0])}")

    save_dataset(problems, args.output)


if __name__ == "__main__":
    main()
