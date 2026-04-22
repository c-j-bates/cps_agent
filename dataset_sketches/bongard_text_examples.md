# Text Bongard Problems: Design & Examples

## What makes a good Bongard problem?

1. Multiple plausible hypotheses can explain the positives alone
2. The negatives are "close" — they rule out the obvious-but-wrong hypotheses  
3. Only one concept cleanly separates positive from negative
4. The concept is *recognizable* once found, but not *obvious* before

For text, we need items that admit many possible descriptions (so the search 
space is non-trivial) but where structural properties are the discriminator.

---

## Type 1: Number Sequences

Each item is a short list of integers. Concept is a structural property.

### Problem N1 (easy) — Concept: strictly increasing
```
Positive: [3, 5, 8]    [1, 2, 3, 4]    [10, 20]    [7, 9, 14, 100]    [2, 6]    [4, 5, 6, 7, 8]
Negative: [3, 5, 3]    [4, 3, 2, 1]    [10, 10]    [7, 14, 9, 100]    [6, 2]    [8, 7, 6, 5, 4]
```
Trap hypothesis: "contains small numbers" (ruled out by negative [4,3,2,1]).

### Problem N2 (medium) — Concept: each element is a multiple of the first
```
Positive: [3, 6, 9]    [5, 10, 25]    [2, 4, 8, 6]    [7, 14]    [4, 12, 8]    [1, 3, 5, 2]
Negative: [3, 7, 9]    [5, 10, 23]    [2, 4, 8, 5]    [7, 15]    [4, 12, 7]    [1, 3, 5, 11]
```
Trap hypothesis: "first element is small" — ruled out by negatives that also start small.
Trap hypothesis: "contains even numbers" — ruled out by [3,6,9] (pos) and [2,4,8,5] (neg).
Correct: every element after the first divides evenly by the first element.

### Problem N3 (hard) — Concept: adjacent differences are strictly increasing
```
Positive: [1, 2, 4, 8]    [10, 11, 13, 17, 25]    [0, 1, 3, 7]    [5, 6, 9, 15]    [20, 21, 24, 31]   [3, 4, 7, 14]
Negative: [1, 2, 4, 7]    [10, 11, 13, 16, 25]    [0, 1, 3, 6]    [5, 6, 9, 14]    [20, 21, 24, 30]   [3, 4, 7, 13]
```
Diffs for first positive: 1,2,4 (increasing). Diffs for first negative: 1,2,3 (also increasing!).
Wait — need to make negatives trickier. Let me revise:

Concept: adjacent differences DOUBLE each time (geometric growth of gaps)
```
Positive: [1, 2, 4, 8]    [10, 11, 13, 17]    [0, 1, 3, 7, 15]    [5, 6, 8, 12]    [20, 21, 23, 27, 35]   [3, 4, 6, 10, 18]
          diffs: 1,2,4      diffs: 1,2,4        diffs: 1,2,4,8      diffs: 1,2,4     diffs: 1,2,4,8         diffs: 1,2,4,8
Negative: [1, 2, 4, 7]    [10, 11, 13, 16]    [0, 1, 3, 7, 14]    [5, 6, 8, 11]    [20, 21, 23, 27, 34]   [3, 4, 6, 10, 17]
          diffs: 1,2,3      diffs: 1,2,3        diffs: 1,2,4,7      diffs: 1,2,3     diffs: 1,2,4,7         diffs: 1,2,4,7
```
Trap: "increasing sequences" — both pos and neg are increasing.
Trap: "diffs are increasing" — both have increasing diffs.
Correct: diffs DOUBLE each time (geometric), not just increase.

---

## Type 2: Character Strings

Each item is a string. Concept is a structural/relational property of characters.

### Problem S1 (easy) — Concept: all characters are unique (no repeats)
```
Positive: "abcde"    "fgh"    "qrst"    "mj"    "zxyw"    "bdfhj"
Negative: "aabcd"    "fgf"    "qrqr"    "mm"    "zxxw"    "bdbhj"
```

### Problem S2 (medium) — Concept: characters form a contiguous run in the alphabet
```
Positive: "abc"    "fgh"    "lm"    "nopqr"    "wx"    "stuv"
Negative: "abd"    "fgi"    "ln"    "nopqs"    "wz"    "stuw"
```
Trap: "short strings" — both contain short strings.
Trap: "characters are in order" — negative "abd" is also in order.
Correct: specifically CONTIGUOUS (no gaps) in the alphabet.

### Problem S3 (hard) — Concept: the string is an interleaving of two sorted subsequences
```
Positive: "acbd"    "aebfcg"    "xaybzc"    "manboc"    "adbe"    "pagbhc"
Negative: "adbc"    "aebfgc"    "xaybcz"    "manbco"    "adeb"    "pahbgc"
```
De-interleave odd/even positions:
- "acbd" -> "ab", "cd" (both sorted) ✓  
- "adbc" -> "ab", "dc" (second not sorted) ✗
Trap: "contains vowels" — irrelevant.
Trap: "characters alternate low/high" — sometimes but not always.
Correct: split into odd-indexed and even-indexed subsequences; both must be sorted.

### Problem S4 (very hard) — Concept: string is a palindrome of CHARACTER CLASSES (vowel=V, consonant=C), not of characters themselves
```
Positive: "abc"(CVC)   "open"(VCVC)   "area"(VCVC)   "dome"(CVCV? wait...)  
```
Hmm, let me reconsider. This needs to be cleaner.

### Problem S4 (very hard, revised) — Concept: the string's consonant-vowel pattern is a palindrome
```
Positive: "aba"(VCV)    "step"(CCVC)    "atom"(VCVC)    "knack"(CCVCC)    "igloo"(VCCVV)    "eve"(VCV)
           VCV=palindrome  CCVC=not...
```
Hmm, CCVC is not a palindrome. Let me fix:

Concept: the CHARACTER COUNT per word matches a palindromic pattern (wrong direction)

Let me try a cleaner hard problem.

### Problem S4 (very hard, revised) — Concept: string has the property that reversing it and shifting each letter by +1 gives back the original
```
This is too contrived. Let me try something else.
```

### Problem S4 (very hard) — Concept: sum of character positions (a=1, b=2, ...) is a prime number
```
Positive: "ab"(1+2=3✓)    "ba"(2+1=3✓)    "ae"(1+5=6✗) WAIT
```
Primes are hard to see from strings. This isn't a "notice the pattern" problem,
it's an arithmetic problem. Bad for Bongard. Let me reconsider.

### Problem S4 (very hard, better) — Concept: the string encodes a valid bracket nesting if you map a-m to "(" and n-z to ")"
```
Positive: "an"    "abno"    "amnop"    "abcnop"    "anbo"    "abnocp"
Negative: "na"    "abno"    "amnpo"    "abcnpo"    "anob"    "abnpco"

Mapping first positive: a→( n→) = "()" valid ✓
Mapping first negative: n→) a→( = ")(" invalid ✗
Second positive: a→( b→( n→) o→) = "(())" valid ✓
Second negative: same as positive by accident... need to fix.
```

Let me step back and think about this more carefully.

---

## Type 3: Token Sequences (best for compositional Bongard)

Use a small vocabulary of abstract tokens. Richer than characters, less noisy
than natural language.

Vocabulary: {alpha, beta, gamma, delta, epsilon, red, blue, green, big, small}

### Problem T1 (easy) — Concept: sequence contains "red"
```
Positive: [alpha, red, beta]    [red, gamma]    [delta, epsilon, red]    
          [red, red]    [alpha, red, gamma, red]    [red, delta]
Negative: [alpha, blue, beta]   [blue, gamma]   [delta, epsilon, green]  
          [blue, green]   [alpha, blue, gamma, green]   [green, delta]
```

### Problem T2 (medium) — Concept: no token appears more than once
```
Positive: [alpha, red, big]    [beta, green]    [gamma, blue, small, delta]    
          [epsilon, red]    [alpha, small]    [delta, green, big]
Negative: [alpha, red, alpha]  [beta, beta]    [gamma, blue, small, gamma]    
          [epsilon, epsilon]   [alpha, small, alpha]    [delta, green, delta]
```

### Problem T3 (hard) — Concept: color tokens and size tokens alternate (color, size, color, size, ...)
```
Positive: [red, big]    [blue, small, green, big]    [red, small]    
          [green, big, red, small]    [blue, big]    [red, big, blue, small, green, big]
Negative: [big, red]    [blue, green, small, big]    [small, red]    
          [green, big, big, small]    [blue, red]    [red, big, blue, big, green, small]

Wait, last negative has color-size alternation: red,big,blue,big -- "blue,big" is color,size
but "big,green" would break it... let me recheck.
Actually [red, big, blue, big, green, small] = color,size,color,size,color,size ✓ 
That's POSITIVE not negative. Let me fix.
```

Negative: [big, red]    [blue, small, big, green]    [small, red]    
          [green, big, red, red]     [big, blue]    [red, small, green, big, blue, big]

---

## Better approach: Procedural generation from a concept DSL

The hand-crafted examples above show the design is fiddly. The right approach
is to define a **concept grammar** and generate problems procedurally.

### Concept DSL for number sequences

```
domain: lists of integers, length 3-6, values 0-100

primitives:
  is_sorted(L)                    # L is in non-decreasing order
  is_strictly_sorted(L)           # L is in strictly increasing order
  all_even(L)                     # every element is even
  all_odd(L)                      # every element is odd
  all_prime(L)                    # every element is prime
  diffs_constant(L)               # arithmetic sequence
  diffs_increasing(L)             # differences are increasing
  ratios_constant(L)              # geometric sequence
  contains_duplicate(L)           # some element repeats
  first_equals_last(L)            # L[0] == L[-1]
  sum_is_even(L)                  # sum of elements is even
  max_minus_min_lt(L, k)          # range is less than k
  length_is(L, k)                 # specific length
  all_divisible_by(L, k)          # all elements divisible by k
  all_less_than(L, k)             # all elements < k
  pairwise_coprime(L)             # GCD of any two elements is 1
  palindrome_sequence(L)          # e.g., [3,5,7,5,3]
  diffs_are_geometric(L)          # differences double (or triple, etc.)
  modular_pattern(L, m, r)        # all elements ≡ r (mod m)

compositions:
  AND(c1, c2)
  NOT(c1)
  IMPLIES(c1, c2)  # less useful but possible
```

### Generation algorithm

```python
def generate_bongard_problem(concept, n_pos=6, n_neg=6, max_attempts=10000):
    """
    concept: a function L -> bool
    
    1. Sample random lists until we have n_pos positives and n_neg negatives.
    2. Filter negatives to be "close" to positives:
       - For each negative, require it to be within edit distance 1 of some positive
         (change one element by a small amount).
       This ensures negatives aren't trivially distinguishable.
    3. Validate: check that no SIMPLER concept in the DSL also separates pos/neg.
       If one does, regenerate (the problem is too easy / ambiguous).
    """
    pass

def generate_with_trap(target_concept, trap_concept, n_pos=6, n_neg=6):
    """
    Generate a problem where:
    - target_concept is the correct separator
    - trap_concept ALSO correctly classifies most (but not all) examples
    - At least one positive satisfies trap_concept=False 
      OR one negative satisfies trap_concept=True
    
    This creates a problem where the solver must consider possibilities 
    beyond the trap to get the right answer.
    """
    pass
```

### Trap depth for Bongard

Similar to the Copycat trap depth, but defined as:

**Trap depth** = number of concepts in the DSL that are strictly simpler than
the correct concept AND correctly classify ≥ (n-1) of the n examples 
(i.e., they "almost work" but fail on one critical example).

This is measurable, generatable, and directly maps to "how many possibilities
must you consider and reject before finding the right one."

---

## Difficulty knobs

1. **Concept complexity**: single primitive vs. AND of two vs. nested
2. **Trap depth**: how many simpler concepts almost-work
3. **Negative closeness**: how similar negatives are to positives (edit distance)
4. **Domain richness**: integers (many features) vs. binary strings (fewer features)
5. **n_examples**: 6 pos / 6 neg (standard) vs. 3/3 (harder) vs. 10/10 (easier)
6. **Distractor features**: add irrelevant variation that must be ignored

---

## Comparison to Copycat analogies

| Dimension | Copycat | Bongard-text |
|-----------|---------|--------------|
| Task | "A→B, C→?" (analogy) | "pos vs neg, find concept" (induction) |
| Hypothesis space | transformations | properties/concepts |
| Ground truth | single answer string | concept (tested on new items) |
| Compositionality | limited (1-2 levels) | tuneable via DSL depth |
| Trap depth control | yes (via group structure) | yes (via concept complexity) |
| Procedural generation | yes | yes |
| Contamination risk | low with counterfactual alphabets | low with random instances |
| Evaluation | exact string match | classify held-out items |

Both are strong candidates. Copycat is better for measuring "abstraction level 
selection." Bongard-text is better for measuring "concept hypothesis generation."
They're complementary.
