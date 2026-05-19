"""
Bongard Text Problem DSL

Defines:
  1. FEATURES: functions from string → value, organized by category
  2. PREDICATES: simple tests on feature values → bool
  3. CLASSIFIERS: predicate(feature(item)) or combinator(pred1(feat1), pred2(feat2))
  4. Generation helpers

Items are strings composed of: a-z, A-Z, 0-9, common symbols.
"""

import math
import string
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Any


# ============================================================================
# FEATURE REGISTRY
# ============================================================================
#
# Each feature extracts a property from a string item.
# Organized by what aspect of the string they attend to.
#
# Convention:
#   - Features return int, float, bool, str, or list
#   - Features that don't apply return None (e.g., digit_sum on "abc")
#   - Names are self-documenting for prompt generation
#

# --- Constants ---

VOWELS = set("aeiouAEIOU")
CONSONANTS = set(string.ascii_letters) - VOWELS

# Visual properties of lowercase letters (approximate, based on standard fonts)
# NOTE: These visual constants are font-dependent and no longer used by the
# generator's feature registry. Kept here for reference only. Known issues:
# the old CURVED_LOWER was missing a, f, m, n, r (font-dependent curves);
# the old STRAIGHT_LOWER had a stray space character.
CURVED_LOWER = set("abcdefgmnopqrsu")  # approximate (font-dependent)
STRAIGHT_LOWER = set("iklvwxz")        # approximate (font-dependent)
ASCENDERS = set("bdfhklt")             # extend above x-height
DESCENDERS = set("gjpqy")             # extend below baseline
# Visually symmetric characters on the vertical axis (left-right mirror).
# NOTE: Font-dependent — no longer used by the generator's feature registry.
# Known to still be incomplete (e.g. lowercase t, y are borderline).
SYMMETRIC_CHARS = set("AHIMOTUVWXYilmouvwx08")
# Characters with enclosed regions (closed loops)
# a=1, b=1, d=1, e=1, g=1(or 2), o=1, p=1, q=1, 0=1, 4=1(some fonts),
# 6=1, 8=2, 9=1, B=2, D=1, O=1, P=1, Q=1, R=1, A=1, #=0
ENCLOSED_REGIONS = {
    "a": 1, "b": 1, "d": 1, "e": 1, "g": 1, "o": 1, "p": 1, "q": 1,
    "A": 1, "B": 2, "D": 1, "O": 1, "P": 1, "Q": 1, "R": 1,
    "0": 1, "4": 1, "6": 1, "8": 2, "9": 1,
}

# Scrabble letter values
SCRABBLE = {
    "a": 1, "b": 3, "c": 3, "d": 2, "e": 1, "f": 4, "g": 2, "h": 4,
    "i": 1, "j": 8, "k": 5, "l": 1, "m": 3, "n": 1, "o": 1, "p": 3,
    "q": 10, "r": 1, "s": 1, "t": 1, "u": 1, "v": 4, "w": 4, "x": 8,
    "y": 4, "z": 10,
}

# Morse code lengths (dots + dashes)
MORSE = {
    "a": 2, "b": 4, "c": 4, "d": 3, "e": 1, "f": 4, "g": 3, "h": 4,
    "i": 2, "j": 4, "k": 3, "l": 4, "m": 2, "n": 2, "o": 3, "p": 4,
    "q": 4, "r": 3, "s": 3, "t": 1, "u": 3, "v": 4, "w": 3, "x": 4,
    "y": 4, "z": 4,
    "0": 5, "1": 5, "2": 5, "3": 5, "4": 5, "5": 5,
    "6": 5, "7": 5, "8": 5, "9": 5,
}


# ---------------------------------------------------------------------------
# A. LENGTH / COUNT FEATURES  (string → int)
# ---------------------------------------------------------------------------

def length(s: str) -> int:
    """Total number of characters."""
    return len(s)

def count_alpha(s: str) -> int:
    """Number of alphabetic characters."""
    return sum(1 for c in s if c.isalpha())

def count_digits(s: str) -> int:
    """Number of digit characters."""
    return sum(1 for c in s if c.isdigit())

def count_symbols(s: str) -> int:
    """Number of non-alphanumeric, non-space characters."""
    return sum(1 for c in s if not c.isalnum() and not c.isspace())

def count_uppercase(s: str) -> int:
    """Number of uppercase letters."""
    return sum(1 for c in s if c.isupper())

def count_lowercase(s: str) -> int:
    """Number of lowercase letters."""
    return sum(1 for c in s if c.islower())

def count_vowels(s: str) -> int:
    """Number of vowels (a, e, i, o, u, case-insensitive)."""
    return sum(1 for c in s if c in VOWELS)

def count_consonants(s: str) -> int:
    """Number of consonants."""
    return sum(1 for c in s if c in CONSONANTS)

def count_unique(s: str) -> int:
    """Number of distinct characters."""
    return len(set(s))

def count_spaces(s: str) -> int:
    """Number of whitespace characters."""
    return sum(1 for c in s if c.isspace())

def max_run_length(s: str) -> int:
    """Length of the longest run of identical characters."""
    if not s:
        return 0
    max_run = cur_run = 1
    for i in range(1, len(s)):
        if s[i] == s[i - 1]:
            cur_run += 1
            max_run = max(max_run, cur_run)
        else:
            cur_run = 1
    return max_run

def num_runs(s: str) -> int:
    """Number of maximal runs of identical characters. 'aabbc' -> 3."""
    if not s:
        return 0
    count = 1
    for i in range(1, len(s)):
        if s[i] != s[i - 1]:
            count += 1
    return count

def count_char_type_transitions(s: str) -> int:
    """Number of transitions between character types (letter/digit/symbol)."""
    def ctype(c):
        if c.isalpha(): return "L"
        if c.isdigit(): return "D"
        return "S"
    if len(s) < 2:
        return 0
    return sum(1 for i in range(1, len(s)) if ctype(s[i]) != ctype(s[i - 1]))


# ---------------------------------------------------------------------------
# B. CHARACTER IDENTITY FEATURES  (string → value)
# ---------------------------------------------------------------------------

def first_char(s: str) -> str | None:
    return s[0] if s else None

def last_char(s: str) -> str | None:
    return s[-1] if s else None

def first_char_type(s: str) -> str | None:
    """Type of first character: 'vowel', 'consonant', 'digit', 'symbol'."""
    if not s:
        return None
    c = s[0]
    if c in VOWELS: return "vowel"
    if c in CONSONANTS: return "consonant"
    if c.isdigit(): return "digit"
    return "symbol"

def last_char_type(s: str) -> str | None:
    if not s:
        return None
    c = s[-1]
    if c in VOWELS: return "vowel"
    if c in CONSONANTS: return "consonant"
    if c.isdigit(): return "digit"
    return "symbol"

def most_common_char(s: str) -> str | None:
    if not s:
        return None
    from collections import Counter
    return Counter(s).most_common(1)[0][0]


# ---------------------------------------------------------------------------
# C. BOOLEAN / STRUCTURAL FEATURES  (string → bool)
# ---------------------------------------------------------------------------

def is_palindrome(s: str) -> bool:
    """String reads the same forwards and backwards."""
    return s == s[::-1]

def all_chars_same(s: str) -> bool:
    return len(set(s)) <= 1

def all_chars_unique(s: str) -> bool:
    return len(set(s)) == len(s)

def first_equals_last(s: str) -> bool:
    return len(s) >= 2 and s[0] == s[-1]

def starts_with_vowel(s: str) -> bool:
    return bool(s) and s[0] in VOWELS

def ends_with_vowel(s: str) -> bool:
    return bool(s) and s[-1] in VOWELS

def starts_with_digit(s: str) -> bool:
    return bool(s) and s[0].isdigit()

def ends_with_digit(s: str) -> bool:
    return bool(s) and s[-1].isdigit()

def has_digits(s: str) -> bool:
    return any(c.isdigit() for c in s)

def has_letters(s: str) -> bool:
    return any(c.isalpha() for c in s)

def has_symbols(s: str) -> bool:
    return any(not c.isalnum() and not c.isspace() for c in s)

def has_repeated_char(s: str) -> bool:
    """At least one character appears more than once."""
    return len(set(s)) < len(s)

def is_alternating_case(s: str) -> bool:
    """Letters alternate upper/lower (ignoring non-letters)."""
    letters = [c for c in s if c.isalpha()]
    if len(letters) < 2:
        return False
    for i in range(1, len(letters)):
        if letters[i].isupper() == letters[i - 1].isupper():
            return False
    return True

def is_alternating_char_type(s: str) -> bool:
    """Alternates between letter and digit (ignoring symbols)."""
    alnum = [c for c in s if c.isalnum()]
    if len(alnum) < 2:
        return False
    for i in range(1, len(alnum)):
        if alnum[i].isdigit() == alnum[i - 1].isdigit():
            return False
    return True


# ---------------------------------------------------------------------------
# D. ALPHABETIC ORDER / POSITION FEATURES
# ---------------------------------------------------------------------------

def alpha_positions(s: str) -> list[int]:
    """List of 1-indexed alphabet positions for each letter (case-insensitive)."""
    return [ord(c.lower()) - ord("a") + 1 for c in s if c.isalpha()]

def alpha_sum(s: str) -> int:
    """Sum of alphabet positions (a=1, ..., z=26)."""
    return sum(alpha_positions(s))

def alpha_product(s: str) -> int:
    """Product of alphabet positions."""
    positions = alpha_positions(s)
    if not positions:
        return 0
    result = 1
    for p in positions:
        result *= p
    return result

def letters_sorted_ascending(s: str) -> bool:
    """Letters are in non-decreasing alphabetical order."""
    pos = alpha_positions(s)
    return all(pos[i] <= pos[i + 1] for i in range(len(pos) - 1)) if len(pos) >= 2 else True

def letters_sorted_descending(s: str) -> bool:
    pos = alpha_positions(s)
    return all(pos[i] >= pos[i + 1] for i in range(len(pos) - 1)) if len(pos) >= 2 else True

def letters_contiguous(s: str) -> bool:
    """Letters form a contiguous run in the alphabet (e.g., 'cde', 'fgh')."""
    pos = sorted(set(alpha_positions(s)))
    if len(pos) < 2:
        return True
    return all(pos[i + 1] - pos[i] == 1 for i in range(len(pos) - 1))

def alpha_diffs(s: str) -> list[int]:
    """Differences between adjacent letter positions."""
    pos = alpha_positions(s)
    return [pos[i + 1] - pos[i] for i in range(len(pos) - 1)]

def alpha_diffs_constant(s: str) -> bool:
    """All differences between adjacent letter positions are equal."""
    d = alpha_diffs(s)
    return len(set(d)) <= 1 if d else True

def alpha_diffs_positive(s: str) -> bool:
    """All adjacent letter diffs are positive (strictly ascending)."""
    d = alpha_diffs(s)
    return all(x > 0 for x in d) if d else True

def alpha_step_size(s: str) -> int | None:
    """If letters are evenly spaced, return the step size; else None."""
    d = alpha_diffs(s)
    if not d:
        return None
    if len(set(d)) == 1:
        return d[0]
    return None


# ---------------------------------------------------------------------------
# E. DIGIT-SPECIFIC FEATURES  (for strings containing digits)
# ---------------------------------------------------------------------------

def digits_of(s: str) -> list[int]:
    """Extract digit values as a list of ints."""
    return [int(c) for c in s if c.isdigit()]

def digit_sum(s: str) -> int:
    return sum(digits_of(s))

def digit_product(s: str) -> int:
    d = digits_of(s)
    if not d:
        return 0
    result = 1
    for x in d:
        result *= x
    return result

def num_even_digits(s: str) -> int:
    return sum(1 for d in digits_of(s) if d % 2 == 0)

def num_odd_digits(s: str) -> int:
    return sum(1 for d in digits_of(s) if d % 2 == 1)

def max_digit(s: str) -> int | None:
    d = digits_of(s)
    return max(d) if d else None

def min_digit(s: str) -> int | None:
    d = digits_of(s)
    return min(d) if d else None

def digit_range(s: str) -> int | None:
    d = digits_of(s)
    return max(d) - min(d) if d else None

def digits_sorted_ascending(s: str) -> bool:
    d = digits_of(s)
    return all(d[i] <= d[i + 1] for i in range(len(d) - 1)) if len(d) >= 2 else True

def digits_sorted_descending(s: str) -> bool:
    d = digits_of(s)
    return all(d[i] >= d[i + 1] for i in range(len(d) - 1)) if len(d) >= 2 else True

def digit_diffs(s: str) -> list[int]:
    d = digits_of(s)
    return [d[i + 1] - d[i] for i in range(len(d) - 1)]

def digit_diffs_constant(s: str) -> bool:
    """Digits form an arithmetic sequence."""
    dd = digit_diffs(s)
    return len(set(dd)) <= 1 if dd else True

def digit_diffs_increasing(s: str) -> bool:
    dd = digit_diffs(s)
    return all(dd[i] < dd[i + 1] for i in range(len(dd) - 1)) if len(dd) >= 2 else True

def digits_all_same_parity(s: str) -> bool:
    d = digits_of(s)
    if len(d) < 2:
        return True
    parities = set(x % 2 for x in d)
    return len(parities) == 1

def all_digits_even(s: str) -> bool:
    """All digits in the string are even (0, 2, 4, 6, 8)."""
    d = digits_of(s)
    if len(d) < 1:
        return False
    return all(x % 2 == 0 for x in d)

def all_digits_odd(s: str) -> bool:
    """All digits in the string are odd (1, 3, 5, 7, 9)."""
    d = digits_of(s)
    if len(d) < 1:
        return False
    return all(x % 2 == 1 for x in d)

def all_letters_vowels(s: str) -> bool:
    """Every alphabetic character is a vowel."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return all(c in VOWELS for c in letters)

def all_letters_consonants(s: str) -> bool:
    """Every alphabetic character is a consonant."""
    letters = [c for c in s if c.isalpha()]
    if not letters:
        return False
    return all(c in CONSONANTS for c in letters)

def digits_alternating_parity(s: str) -> bool:
    """Even-odd-even-odd or odd-even-odd-even."""
    d = digits_of(s)
    if len(d) < 2:
        return True
    return all((d[i] % 2) != (d[i + 1] % 2) for i in range(len(d) - 1))

def opposite_parity_digits(s: str) -> bool:
    """For 2-digit numbers: the two digits have different parity."""
    d = digits_of(s)
    if len(d) != 2:
        return False
    return (d[0] % 2) != (d[1] % 2)


# ---------------------------------------------------------------------------
# F. NUMERIC VALUE FEATURES  (when the string IS a number)
# ---------------------------------------------------------------------------

def numeric_value(s: str) -> int | None:
    """Parse as integer if possible."""
    stripped = s.strip()
    try:
        return int(stripped)
    except ValueError:
        return None

def is_prime_number(s: str) -> bool:
    """Is the numeric value prime?"""
    n = numeric_value(s)
    if n is None or n < 2:
        return False
    if n < 4:
        return True
    if n % 2 == 0 or n % 3 == 0:
        return False
    i = 5
    while i * i <= n:
        if n % i == 0 or n % (i + 2) == 0:
            return False
        i += 6
    return True

def is_perfect_square(s: str) -> bool:
    n = numeric_value(s)
    if n is None or n < 0:
        return False
    root = int(math.isqrt(n))
    return root * root == n

def is_power_of_2(s: str) -> bool:
    n = numeric_value(s)
    if n is None or n < 1:
        return False
    return (n & (n - 1)) == 0

def is_fibonacci(s: str) -> bool:
    """Is the value a Fibonacci number?"""
    n = numeric_value(s)
    if n is None or n < 0:
        return False
    # n is Fibonacci iff 5n^2+4 or 5n^2-4 is a perfect square
    def is_sq(x):
        if x < 0: return False
        r = int(math.isqrt(x))
        return r * r == x
    return is_sq(5 * n * n + 4) or is_sq(5 * n * n - 4)

def num_divisors(s: str) -> int | None:
    n = numeric_value(s)
    if n is None or n < 1:
        return None
    count = 0
    for i in range(1, int(math.isqrt(n)) + 1):
        if n % i == 0:
            count += 2 if i != n // i else 1
    return count

def is_palindrome_number(s: str) -> bool:
    n = numeric_value(s)
    if n is None:
        return False
    ns = str(abs(n))
    return ns == ns[::-1]

def binary_digit_count(s: str) -> int | None:
    """Number of 1s in binary representation."""
    n = numeric_value(s)
    if n is None:
        return None
    return bin(abs(n)).count("1")

def binary_length(s: str) -> int | None:
    """Length of binary representation (excluding '0b')."""
    n = numeric_value(s)
    if n is None:
        return None
    return len(bin(abs(n))) - 2


# ---------------------------------------------------------------------------
# G. ENCODING / REPRESENTATION FEATURES
# ---------------------------------------------------------------------------

def ascii_sum(s: str) -> int:
    """Sum of ASCII values of all characters."""
    return sum(ord(c) for c in s)

def scrabble_score(s: str) -> int:
    """Sum of Scrabble tile values (letters only, case-insensitive)."""
    return sum(SCRABBLE.get(c.lower(), 0) for c in s)

def morse_length(s: str) -> int:
    """Total dots + dashes in Morse code encoding."""
    return sum(MORSE.get(c.lower(), 0) for c in s)

def roman_numeral_length(s: str) -> int | None:
    """Length of Roman numeral representation (for integers 1-3999)."""
    n = numeric_value(s)
    if n is None or n < 1 or n > 3999:
        return None
    vals = [
        (1000, "M"), (900, "CM"), (500, "D"), (400, "CD"),
        (100, "C"), (90, "XC"), (50, "L"), (40, "XL"),
        (10, "X"), (9, "IX"), (5, "V"), (4, "IV"), (1, "I"),
    ]
    result = ""
    for value, numeral in vals:
        while n >= value:
            result += numeral
            n -= value
    return len(result)

def count_enclosed_regions(s: str) -> int:
    """Count enclosed regions / loops in the visual form of all characters."""
    return sum(ENCLOSED_REGIONS.get(c, 0) for c in s)

def count_curved_letters(s: str) -> int:
    return sum(1 for c in s if c.lower() in CURVED_LOWER)

def count_ascenders(s: str) -> int:
    return sum(1 for c in s if c.lower() in ASCENDERS)

def count_descenders(s: str) -> int:
    return sum(1 for c in s if c.lower() in DESCENDERS)

def count_symmetric_chars(s: str) -> int:
    return sum(1 for c in s if c in SYMMETRIC_CHARS)


# ---------------------------------------------------------------------------
# H. PATTERN / PERIODICITY FEATURES
# ---------------------------------------------------------------------------

def cv_pattern(s: str) -> str:
    """Consonant/vowel pattern. 'hello' -> 'CVCCV'. Non-letters omitted."""
    return "".join("V" if c in VOWELS else "C" for c in s if c.isalpha())

def type_pattern(s: str) -> str:
    """Character type pattern. 'a1b!' -> 'LDLS'."""
    result = []
    for c in s:
        if c.isalpha():
            result.append("L")
        elif c.isdigit():
            result.append("D")
        else:
            result.append("S")
    return "".join(result)

def case_pattern(s: str) -> str:
    """Upper/lower pattern for letters. 'HeLLo' -> 'ULLLU'."""
    return "".join("U" if c.isupper() else "L" for c in s if c.isalpha())

def is_periodic(s: str) -> bool:
    """String is k repetitions of a shorter substring (k >= 2)."""
    n = len(s)
    for period in range(1, n // 2 + 1):
        if n % period == 0:
            unit = s[:period]
            if unit * (n // period) == s:
                return True
    return False

def shortest_period(s: str) -> int:
    """Length of shortest repeating unit. Returns len(s) if not periodic."""
    n = len(s)
    for period in range(1, n + 1):
        if n % period == 0:
            unit = s[:period]
            if unit * (n // period) == s:
                return period
    return n

def cv_pattern_is_palindrome(s: str) -> bool:
    """The consonant-vowel pattern is itself a palindrome."""
    p = cv_pattern(s)
    return p == p[::-1]

def has_repeated_bigram(s: str) -> bool:
    """Contains a repeated 2-character substring."""
    if len(s) < 4:
        return False
    bigrams = [s[i:i+2] for i in range(len(s) - 1)]
    return len(bigrams) != len(set(bigrams))


# ---------------------------------------------------------------------------
# I. CROSS-TYPE / RATIO FEATURES
# ---------------------------------------------------------------------------

def vowel_ratio(s: str) -> float:
    """Fraction of alphabetic characters that are vowels."""
    alpha = count_alpha(s)
    if alpha == 0:
        return 0.0
    return count_vowels(s) / alpha

def digit_letter_ratio(s: str) -> float | None:
    """Ratio of digits to letters. None if no letters."""
    letters = count_alpha(s)
    if letters == 0:
        return None
    return count_digits(s) / letters

def uppercase_ratio(s: str) -> float:
    """Fraction of letters that are uppercase."""
    alpha = count_alpha(s)
    if alpha == 0:
        return 0.0
    return count_uppercase(s) / alpha

def unique_ratio(s: str) -> float:
    """Fraction of characters that are unique."""
    if not s:
        return 0.0
    return count_unique(s) / len(s)


# ============================================================================
# PREDICATE DSL
# ============================================================================
#
# Predicates test feature values and return bool.
# These are the "simple functions" in the concept DSL.
#

def eq(val, target):
    """Equals."""
    return val == target

def neq(val, target):
    return val != target

def gt(val, target):
    return val is not None and val > target

def lt(val, target):
    return val is not None and val < target

def gte(val, target):
    return val is not None and val >= target

def lte(val, target):
    return val is not None and val <= target

def between(val, lo, hi):
    return val is not None and lo <= val <= hi

def is_even(val):
    return val is not None and val % 2 == 0

def is_odd(val):
    return val is not None and val % 2 == 1

def _is_prime(val):
    if val is None or val < 2:
        return False
    if val < 4:
        return True
    if val % 2 == 0:
        return False
    for i in range(3, int(math.isqrt(val)) + 1, 2):
        if val % i == 0:
            return False
    return True

def divisible_by(val, k):
    return val is not None and k != 0 and val % k == 0

def modulo_eq(val, m, r):
    return val is not None and m != 0 and val % m == r


# ============================================================================
# COMBINATORS
# ============================================================================

def AND(p1: bool, p2: bool) -> bool:
    return p1 and p2

def OR(p1: bool, p2: bool) -> bool:
    return p1 or p2

def NOT(p: bool) -> bool:
    return not p


# ============================================================================
# CLASSIFIER = predicate(feature(item))  or  combinator(p1(f1(item)), p2(f2(item)))
# ============================================================================

@dataclass
class Classifier:
    """
    A ground-truth positive/negative classifier for Bongard problems.

    Simple form:  predicate(feature(item))
    Composed:     combinator(pred1(feat1(item)), pred2(feat2(item)))

    depth=0: single predicate on single feature
    depth=1: combinator of two depth-0 classifiers (AND, OR, NOT)
    """
    description: str              # human-readable description of the concept
    classify: Callable[[str], bool]  # the actual classifier function
    depth: int                    # 0 = single, 1 = composed
    feature_names: list[str]      # which features are used (for trap analysis)
    difficulty_hint: str          # 'easy', 'medium', 'hard', 'very_hard'


# ============================================================================
# EXAMPLE CLASSIFIERS
# ============================================================================
#
# These illustrate the DSL's expressive range.
# A generator would sample from this space.
#

EXAMPLE_CLASSIFIERS = [

    # ------ DEPTH 0: single feature, simple predicate ------

    # Easy: surface-level, obvious feature
    Classifier(
        description="string length is even",
        classify=lambda s: is_even(length(s)),
        depth=0,
        feature_names=["length"],
        difficulty_hint="easy",
    ),
    Classifier(
        description="string is a palindrome",
        classify=lambda s: is_palindrome(s),
        depth=0,
        feature_names=["is_palindrome"],
        difficulty_hint="easy",
    ),
    Classifier(
        description="starts with a vowel",
        classify=lambda s: starts_with_vowel(s),
        depth=0,
        feature_names=["starts_with_vowel"],
        difficulty_hint="easy",
    ),
    Classifier(
        description="all characters are unique",
        classify=lambda s: all_chars_unique(s),
        depth=0,
        feature_names=["all_chars_unique"],
        difficulty_hint="easy",
    ),

    # Medium: requires attending to a less-obvious property
    Classifier(
        description="number of vowels exceeds number of consonants",
        classify=lambda s: count_vowels(s) > count_consonants(s),
        depth=0,
        feature_names=["count_vowels", "count_consonants"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="digit sum is even",
        classify=lambda s: is_even(digit_sum(s)),
        depth=0,
        feature_names=["digit_sum"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="letters form a contiguous alphabetic run",
        classify=lambda s: letters_contiguous(s),
        depth=0,
        feature_names=["letters_contiguous"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="the two digits have opposite parity",
        classify=lambda s: opposite_parity_digits(s),
        depth=0,
        feature_names=["opposite_parity_digits"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="consonant-vowel pattern is a palindrome",
        classify=lambda s: cv_pattern_is_palindrome(s),
        depth=0,
        feature_names=["cv_pattern_is_palindrome"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="string has a repeating period (e.g., 'abcabc')",
        classify=lambda s: is_periodic(s),
        depth=0,
        feature_names=["is_periodic"],
        difficulty_hint="medium",
    ),

    # Hard: requires a non-obvious MAPPING before a simple test
    Classifier(
        description="scrabble score is a prime number",
        classify=lambda s: _is_prime(scrabble_score(s)),
        depth=0,
        feature_names=["scrabble_score"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="number of enclosed regions (loops in letter shapes) is even",
        classify=lambda s: is_even(count_enclosed_regions(s)),
        depth=0,
        feature_names=["count_enclosed_regions"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="roman numeral representation has odd length",
        classify=lambda s: roman_numeral_length(s) is not None and is_odd(roman_numeral_length(s)),
        depth=0,
        feature_names=["roman_numeral_length"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="morse code total length is divisible by 3",
        classify=lambda s: divisible_by(morse_length(s), 3),
        depth=0,
        feature_names=["morse_length"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="ASCII sum is a perfect square",
        classify=lambda s: is_perfect_square(str(ascii_sum(s))),
        depth=0,
        feature_names=["ascii_sum"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="number of visually symmetric characters exceeds non-symmetric",
        classify=lambda s: count_symmetric_chars(s) > len(s) - count_symmetric_chars(s),
        depth=0,
        feature_names=["count_symmetric_chars"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="sum of alphabet positions is divisible by the string length",
        classify=lambda s: length(s) > 0 and divisible_by(alpha_sum(s), length(s)),
        depth=0,
        feature_names=["alpha_sum", "length"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="the number of 1-bits in the binary representation is odd",
        classify=lambda s: binary_digit_count(s) is not None and is_odd(binary_digit_count(s)),
        depth=0,
        feature_names=["binary_digit_count"],
        difficulty_hint="hard",
    ),

    # Very hard: obscure mapping + non-trivial test
    Classifier(
        description="number of descenders minus ascenders equals 1",
        classify=lambda s: count_descenders(s) - count_ascenders(s) == 1,
        depth=0,
        feature_names=["count_descenders", "count_ascenders"],
        difficulty_hint="very_hard",
    ),
    Classifier(
        description="digit sum equals count of curved letters",
        classify=lambda s: digit_sum(s) == count_curved_letters(s),
        depth=0,
        feature_names=["digit_sum", "count_curved_letters"],
        difficulty_hint="very_hard",
    ),

    # ------ DEPTH 1: composed classifiers ------

    Classifier(
        description="palindrome AND length is odd",
        classify=lambda s: is_palindrome(s) and is_odd(length(s)),
        depth=1,
        feature_names=["is_palindrome", "length"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="starts with vowel AND ends with digit",
        classify=lambda s: starts_with_vowel(s) and ends_with_digit(s),
        depth=1,
        feature_names=["starts_with_vowel", "ends_with_digit"],
        difficulty_hint="medium",
    ),
    Classifier(
        description="letters sorted ascending AND digit sum is even",
        classify=lambda s: letters_sorted_ascending(s) and is_even(digit_sum(s)),
        depth=1,
        feature_names=["letters_sorted_ascending", "digit_sum"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="NOT periodic AND has repeated bigram",
        classify=lambda s: not is_periodic(s) and has_repeated_bigram(s),
        depth=1,
        feature_names=["is_periodic", "has_repeated_bigram"],
        difficulty_hint="hard",
    ),
    Classifier(
        description="count of enclosed regions > 2 AND first char is symmetric",
        classify=lambda s: count_enclosed_regions(s) > 2 and (s[0] in SYMMETRIC_CHARS if s else False),
        depth=1,
        feature_names=["count_enclosed_regions", "count_symmetric_chars"],
        difficulty_hint="very_hard",
    ),
]


# ============================================================================
# FEATURE REGISTRY (for programmatic enumeration)
# ============================================================================

FEATURE_REGISTRY = {
    # Category A: Length / Count
    "length": (length, int, "Length/count"),
    "count_alpha": (count_alpha, int, "Length/count"),
    "count_digits": (count_digits, int, "Length/count"),
    "count_symbols": (count_symbols, int, "Length/count"),
    "count_uppercase": (count_uppercase, int, "Length/count"),
    "count_lowercase": (count_lowercase, int, "Length/count"),
    "count_vowels": (count_vowels, int, "Length/count"),
    "count_consonants": (count_consonants, int, "Length/count"),
    "count_unique": (count_unique, int, "Length/count"),
    "max_run_length": (max_run_length, int, "Length/count"),
    "num_runs": (num_runs, int, "Length/count"),
    "count_char_type_transitions": (count_char_type_transitions, int, "Length/count"),

    # Category B: Character identity
    "first_char_type": (first_char_type, str, "Character identity"),
    "last_char_type": (last_char_type, str, "Character identity"),

    # Category C: Boolean / Structural
    "is_palindrome": (is_palindrome, bool, "Structure"),
    "all_chars_same": (all_chars_same, bool, "Structure"),
    "all_chars_unique": (all_chars_unique, bool, "Structure"),
    "first_equals_last": (first_equals_last, bool, "Structure"),
    "starts_with_vowel": (starts_with_vowel, bool, "Structure"),
    "ends_with_vowel": (ends_with_vowel, bool, "Structure"),
    "starts_with_digit": (starts_with_digit, bool, "Structure"),
    "ends_with_digit": (ends_with_digit, bool, "Structure"),
    "has_digits": (has_digits, bool, "Structure"),
    "has_letters": (has_letters, bool, "Structure"),
    "has_symbols": (has_symbols, bool, "Structure"),
    "has_repeated_char": (has_repeated_char, bool, "Structure"),
    "is_alternating_case": (is_alternating_case, bool, "Structure"),
    "is_alternating_char_type": (is_alternating_char_type, bool, "Structure"),

    # Category D: Alphabetic order / position
    "alpha_sum": (alpha_sum, int, "Alphabetic"),
    "alpha_product": (alpha_product, int, "Alphabetic"),
    "letters_sorted_ascending": (letters_sorted_ascending, bool, "Alphabetic"),
    "letters_sorted_descending": (letters_sorted_descending, bool, "Alphabetic"),
    "letters_contiguous": (letters_contiguous, bool, "Alphabetic"),
    "alpha_diffs_constant": (alpha_diffs_constant, bool, "Alphabetic"),
    "alpha_diffs_positive": (alpha_diffs_positive, bool, "Alphabetic"),

    # Category E: Digit-specific
    "digit_sum": (digit_sum, int, "Digit"),
    "digit_product": (digit_product, int, "Digit"),
    "num_even_digits": (num_even_digits, int, "Digit"),
    "num_odd_digits": (num_odd_digits, int, "Digit"),
    "max_digit": (max_digit, int, "Digit"),
    "min_digit": (min_digit, int, "Digit"),
    "digit_range": (digit_range, int, "Digit"),
    "digits_sorted_ascending": (digits_sorted_ascending, bool, "Digit"),
    "digits_sorted_descending": (digits_sorted_descending, bool, "Digit"),
    "digit_diffs_constant": (digit_diffs_constant, bool, "Digit"),
    # digit_diffs_increasing removed: uses SIGNED differences, but the
    # natural-language description ("gaps are increasing") implies absolute.
    # Solver would pursue absolute and fail. The signed-diffs-increasing
    # concept is too esoteric for a Bongard problem.
    "digits_all_same_parity": (digits_all_same_parity, bool, "Digit"),
    "digits_alternating_parity": (digits_alternating_parity, bool, "Digit"),
    "all_digits_even": (all_digits_even, bool, "Digit"),
    "all_digits_odd": (all_digits_odd, bool, "Digit"),
    "all_letters_vowels": (all_letters_vowels, bool, "Alphabetic"),
    "all_letters_consonants": (all_letters_consonants, bool, "Alphabetic"),
    "opposite_parity_digits": (opposite_parity_digits, bool, "Digit"),

    # Category F: Numeric value
    "is_prime_number": (is_prime_number, bool, "Numeric"),
    "is_perfect_square": (is_perfect_square, bool, "Numeric"),
    "is_power_of_2": (is_power_of_2, bool, "Numeric"),
    "is_fibonacci": (is_fibonacci, bool, "Numeric"),
    "num_divisors": (num_divisors, int, "Numeric"),
    "is_palindrome_number": (is_palindrome_number, bool, "Numeric"),
    "binary_digit_count": (binary_digit_count, int, "Numeric"),
    "binary_length": (binary_length, int, "Numeric"),

    # Category G: Encoding / Representation
    "ascii_sum": (ascii_sum, int, "Encoding"),
    "scrabble_score": (scrabble_score, int, "Encoding"),
    "morse_length": (morse_length, int, "Encoding"),
    "roman_numeral_length": (roman_numeral_length, int, "Encoding"),
    # NOTE: Visually-inferred features removed from the registry because they
    # are font-dependent and conflate "visual recognition knowledge" with
    # the cognitive axis we want to measure (considering possibilities).
    # The function definitions are kept for possible reuse but are no longer
    # emitted as candidate classifiers:
    #   count_enclosed_regions, count_curved_letters,
    #   count_ascenders, count_descenders, count_symmetric_chars

    # Category H: Pattern / Periodicity
    "cv_pattern_is_palindrome": (cv_pattern_is_palindrome, bool, "Pattern"),
    "is_periodic": (is_periodic, bool, "Pattern"),
    "shortest_period": (shortest_period, int, "Pattern"),
    "has_repeated_bigram": (has_repeated_bigram, bool, "Pattern"),

    # Category I: Cross-type / Ratio
    "vowel_ratio": (vowel_ratio, float, "Ratio"),
    "uppercase_ratio": (uppercase_ratio, float, "Ratio"),
    "unique_ratio": (unique_ratio, float, "Ratio"),
}

# Difficulty ranking of feature categories
# (how likely a model is to consider features in this category)
CATEGORY_DIFFICULTY = {
    "Length/count": 1,      # very likely to consider
    "Character identity": 1,
    "Structure": 2,         # likely
    "Alphabetic": 2,
    "Digit": 2,
    "Numeric": 3,           # moderate
    "Pattern": 3,
    "Ratio": 3,
    "Encoding": 4,          # unlikely — requires non-obvious mapping
}


# ============================================================================
# NOTES ON GENERATION
# ============================================================================
#
# To generate a Bongard problem:
#
# 1. PICK A CLASSIFIER from the DSL (or sample one):
#    - Choose a feature (or pair of features)
#    - Choose a predicate over that feature
#    - Compose into a classifier
#
# 2. GENERATE ITEMS:
#    - Sample random strings from the item space
#    - Classify each with the ground-truth classifier
#    - Collect 6 positive and 6 negative
#
# 3. ENSURE CLOSE NEGATIVES:
#    - For each positive, generate a "close" negative by minimal perturbation
#      (swap one character, change case, add/remove one char, etc.)
#    - This ensures that surface-level features don't trivially separate
#
# 4. VALIDATE (optional but recommended):
#    - Check that no SIMPLER classifier in the DSL also separates all examples
#    - If one does, the problem is ambiguous (or too easy) — regenerate
#    - "Simpler" = lower depth, or fewer features, or more common category
#
# 5. COMPUTE TRAP DEPTH:
#    - Count how many simpler classifiers correctly classify >= (n-1) examples
#    - These are the "almost-right" hypotheses the solver must consider and reject
#    - trap_depth = number of such classifiers
#
# Item space options:
#   - Pure lowercase alpha: [a-z]{3,8}      (for alphabetic features)
#   - Pure numeric: [0-9]{2,5}               (for digit/numeric features)
#   - Mixed alphanum: [a-zA-Z0-9]{3,8}      (for cross-type features)
#   - With symbols: [printable]{3,10}        (for symbol-counting features)
#   - Two-digit numbers: [1-9][0-9]          (for Quanta-style problems)
#   - Words: sample from a word list         (for encoding features)
#
