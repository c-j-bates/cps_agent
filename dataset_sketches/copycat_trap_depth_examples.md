# Counterfactual Analogy Fork: Trap-Depth Examples

## Definitions

**Trap depth** = number of simpler-than-correct hypotheses that are consistent with
the demonstration pair (A -> B) but give the wrong answer for C -> ?

Standard alphabet unless noted.  Permuted alphabet noted where used.

---

## Trap Depth 0 (no trap; obvious answer is correct)

### TD0-1: Simple successor of last
```
A: a b c   ->   B: a b d
C: p q r   ->   ?
Answer: p q s
```
Only one natural reading: replace last letter with its successor. No competing hypothesis.

### TD0-2: Remove last element
```
A: m n o p   ->   B: m n o
C: w x y z   ->   ?
Answer: w x y
```

---

## Trap Depth 1 (one tempting-but-wrong hypothesis)

### TD1-1: Letter-level vs. group-level successor
```
A: a b c       ->   B: a b d
C: ii jj kk   ->   ?
Trap (depth 0): ii jj kl      (successor of last LETTER)
Correct:        ii jj ll      (successor of last GROUP)
```
The model must consider "groups as atoms" to get this right. The letter-level
reading is simpler and consistent with A->B.

### TD1-2: Last-letter vs. last-distinct-letter successor
```
A: d e f   ->   B: d e g
C: r s s   ->   ?
Trap (depth 0): r s t      (successor of final character 's')
Correct:        r t t      (successor of the last DISTINCT letter-type, changing all instances? 
                            Or: the rightmost novel letter is 's', its successor 't' replaces)
```
Note: this one has genuine ambiguity. Might want multiple demonstration pairs
to disambiguate. See "demonstration count" axis below.

### TD1-3: Successor with counterfactual alphabet
```
Alphabet: a c b d f e g i h j ...  (swap every 2nd-3rd pair)
A: a c b   ->   B: a c d       (successor of b in this alphabet is d)
C: g i h   ->   ?
Trap (depth 0): g i i           (standard alphabet: successor of h is i)
Correct:        g i j           (in this alphabet, successor of h is j)
```

---

## Trap Depth 2 (two tempting-but-wrong hypotheses)

### TD2-1: Letter-level vs. group-level vs. meta-pattern
```
A: a b c       ->   B: a b d
C: m rr jjj   ->   ?
Trap 1 (letter-level):   m rr jjk       (successor of last letter j -> k)
Trap 2 (group-level):    m rr kkk       (successor of last group: jjj -> kkk)
Correct (meta-pattern):  m rr jjj kkkk  (the pattern is groups of increasing length
                                          1,2,3 -> next group is length 4, letters 
                                          continue alphabetically: j->k, so kkkk)
```
Requires noticing the *structure across groups* (lengths 1,2,3), not just within them.

### TD2-2: Fintz + group structure
```
A: a b c       ->   B: a b d
C: xx yy zz   ->   ?
Trap 1 (letter-level):   xx yy z?   (successor of z is undefined)
Trap 2 (group-level):    xx yy ??   (successor of zz-group, but z has no successor)
Correct:                 xx zz zz   OR  yy yy zz   
    (must shift abstraction: e.g., "increment" can't apply, so try 
     "replace last distinct group with successor of second-to-last group" 
     or "lengthen the string". Multiple defensible answers -- 
     this is the classic Copycat "xyz problem".)
```

### TD2-3: Counterfactual alphabet + group-level
```
Alphabet: z y x w v u t s r q p o n m l k j i h g f e d c b a  (reversed)
A: z y x   ->   B: z y w    (successor of x in reversed alphabet = w)
C: nn mm ll  ->  ?
Trap 1 (letter-level, standard alphabet): nn mm lm   (successor of l = m, but wrong alphabet)
Trap 2 (letter-level, correct alphabet):  nn mm lk   (successor of l in reversed = k)
Correct (group-level, correct alphabet):  nn mm kk   (successor of ll-group in reversed alphabet)
```

---

## Trap Depth 3 (three layers)

### TD3-1: The full stack
```
Alphabet: d a c b f e h g j i   (swap pairs: ab->da, cd->cb, ef->fe, gh->hg, ij->ji)
A: d a c      ->   B: d a b      
    (what happened: 'c' -> its successor in this alphabet = 'b')
C: hh gg jj   ->   ?
Trap 1 (letter, standard alphabet): hh gg jk   (j -> k standard)
Trap 2 (letter, correct alphabet):  hh gg ji   (j -> i in this alphabet)
Trap 3 (group, standard alphabet):  hh gg kk   
Correct (group, correct alphabet):  hh gg ii   (jj -> ii, group-level, correct alphabet)
```

---

## Procedural generation recipe

```
Parameters:
  - alphabet: standard | permuted(n) | reversed | symbolic
  - structure: atomic | grouped(uniform) | grouped(increasing) | grouped(mixed)
  - transformation: successor | predecessor | remove | duplicate | reverse | swap
  - trap_depth: 0 | 1 | 2 | 3
  - n_demonstrations: 1 | 2 | 3  (more demos -> less ambiguity)
  - fintz: false | true  (boundary case that blocks the default operation)

Generation:
  1. Sample parameters.
  2. Define the "correct" transformation at the intended abstraction level.
  3. Generate (A, B) pair that is ALSO consistent with all shallower abstractions.
     (This is the key constraint that creates the trap.)
  4. Generate C such that shallower abstractions give DIFFERENT answers than the correct one.
  5. Record: problem, correct_answer, list_of_trap_answers (ordered by depth).

Verification:
  - Human norming on a pilot set to confirm intended abstraction is recoverable.
  - Check that trap answers are genuinely distinct strings.
```

## Difficulty dial

The core insight: **trap depth directly measures the depth of possibility-search
required.** A model that only considers the shallowest hypothesis will fail at
TD >= 1. A model that considers two levels will fail at TD >= 2. Etc.

This gives a clean, parametric experimental design:
  - IV: trap depth (0, 1, 2, 3)
  - IV: alphabet type (standard vs. counterfactual)
  - DV: accuracy
  - Prediction: accuracy decreases monotonically with trap depth;
    the CPS scaffold should shift the curve rightward (extend the depth 
    at which models still succeed).
