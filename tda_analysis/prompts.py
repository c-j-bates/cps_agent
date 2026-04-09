"""
LLM prompt templates for the TDA creativity pipeline.

Prompt A: Initial coding (solver output → tree trace)
Prompt B: Deduplication & normalization
Prompt C: Novel type review (batch)
"""

PROMPT_A = r'''
CLUE: "{clue_text}" ({clue_enum})

SOLVER OUTPUT:
{llm_outputs}

## YOUR TASK
Analyze the solver output and produce an ordered trace of ideas
as Python code. Each idea is a tree-building operation that records
a commitment the solver made about how the clue works.

## OUTPUT FORMAT
Produce ONLY Python code in the following format:

```python
# Key phrase: "<quote from solver output>"
trace.append(("<tree_id>", <parent_path>, {{
    "parse":     <parse_value_or_None>,
    "mechanism": <mechanism_value_or_None>,
    "execution": <execution_value_or_None>,
    "output":    <output_value_or_None>,
}}))
```

Where:
- tree_id: string like "tree1", "tree2", etc. Start a new tree
  when the solver begins a completely unrelated line of reasoning.
  If the solver returns to a previous line, reuse that tree_id.
- parent_path: list of child indices from root, e.g. [] for root
  level, [0] for first child of first root node, [0,1] for second
  child of that node, etc.
- The 4 facets (any may be None):

### FACET 1: parse
Identifies the definition portion of the clue.
  {{"position": "start", "text": "<substring>"}}
  {{"position": "end",   "text": "<substring>"}}
  {{"position": "whole"}}  # for &lit or cryptic_def
  {{"position": "other",  "text": "<substring>"}}  # rare mid-clue def
  None  # solver did not commit to a parse

### FACET 2: mechanism
Ordered list of wordplay types, in APPLICATION order.
ALWAYS use standard types from this list first:

  anagram       - Letters rearranged. Indicators: mixed, broken,
                  confused, twisted, wild, reformed, shattered,
                  dancing, drunk, crazy, ruined, destroyed,
                  scrambled, muddled, chaotic, cooked, novel, odd,
                  strange, rebuilt, mangled, wrecked, etc.
  hidden_word   - Answer concealed in consecutive letters.
                  Indicators: in, within, part of, some, found in,
                  bit of, contains, holds, sample of, buried in.
  reversal      - Spelled backward. Indicators: back, returns,
                  reversed, about, over, up (down clues), reflected.
  container     - One word inside another. Indicators: around,
                  holds, embraces, swallows, outside, keeping.
                  "A in B" = A inside B. "A around B" = A wraps B.
  deletion      - Letters removed. Subtypes:
                  beheadment (first), curtailment (last),
                  middle_deletion, specific_deletion.
                  Indicators: without, losing, headless, topless,
                  endless, short, curtailed, hollow, drops, lacks.
  charade       - Parts joined in sequence. Indicators: with, and,
                  after, follows, next to, on, beside, plus.
                  Often NO indicator — juxtaposition is the signal.
  double_def    - Two separate definitions, no wordplay. Usually
                  short clues with no indicators.
  homophone     - Sounds like another word. Indicators: sounds
                  like, we hear, say, reportedly, audibly, aloud.
  cryptic_def   - Entire clue is a punning definition. Often ends
                  with ?. No separate wordplay.
  &lit          - Entire clue is BOTH definition AND wordplay.
                  Often ends with !.
  acrostic      - Initial/final/nth letters spell answer.
                  Indicators: initially, first, primarily, leaders.
  substitution  - Letter/substring replaced by another.
                  Indicators: for, in place of, replacing.
  letter_selection - Positional letters picked. Indicators: odd,
                  even, regularly, alternately, middle of, heart of.

If the solver describes something that does not fit ANY of the
above types, introduce a new snake_case type name with a comment
explaining it, and prefix the name with "novel_".
Example: "novel_palindrome"  # answer reads same forwards/back

For compound clues, use a list: ["deletion", "anagram"]
Order = order of application, NOT order in clue text.

### FACET 3: execution
List of step dicts, one per mechanism step:
  [{{"indicator": {{"text": "<quoted from clue>"}},
    "fodder":    {{"text": "<quoted from clue>"}},
    "result":    "<UPPERCASE>"}}]

For deletions, also include "delete": "<letters removed>".
For containers, include "outer"/"inner" instead of just "fodder".
Use the EXACT text from the clue for "text" fields.
None if the solver identified a mechanism but did not work out
specifics.

### FACET 4: output
The answer guess as an UPPERCASE string, or None.

## COMMON ABBREVIATIONS (for recognizing charade/substitution fodder)
Compass: N,S,E,W. Roman numerals: I,V,X,L,C,D,M.
Doctor: DR,MO. Sailor: AB,TAR. About: RE,CA. Street: ST,RD.
Nothing: O,NIL. One: I,A,AN. Editor: ED. Energy: E. 50: L.
Musical notes: DO,RE,MI,FA,SO,LA,TI. 100: C,TON.

## RULES
1. Go line by line through the solver output. Record every new
   idea as it appears. Preserve chronological order.
2. If the solver revisits a previous line of reasoning, use the
   same tree_id and extend from the appropriate parent_path.
3. If the solver uses non-standard terminology (e.g., "mix up
   the letters"), map to the closest standard mechanism type.
4. Populate ONLY the facets you can confidently identify from
   the text. Leave others as None.
5. If the solver explicitly rejects an idea, still record it
   (annotate with # REJECTED).
6. Do NOT skip the output facet: whenever the solver produces
   a concrete guess, record it.
7. A solver statement that populates multiple facets at once
   is ONE trace entry with multiple non-None facets.
'''.strip()


PROMPT_B = r'''
CLUE: "{clue_text}"

RAW TRACE:
{raw_trace}

## YOUR TASK
Clean the trace by applying these operations IN ORDER:

### STEP 1: Mechanism normalization
For every mechanism value in the trace:
- Map to the closest standard type from this list:
  anagram, hidden_word, reversal, container, deletion,
  charade, double_def, homophone, cryptic_def, &lit,
  acrostic, substitution, letter_selection.
- If a value is a non-standard synonym (e.g., "reverse" →
  "reversal", "hidden" → "hidden_word"), remap it.
- If it genuinely does not fit any standard type, keep the
  novel_ prefix and add a comment.
- Ensure compound mechanisms are in APPLICATION order.

### STEP 2: Text normalization
- All "text" fields in parse and execution: preserve exact
  casing and punctuation from the original clue. Trim
  leading/trailing whitespace only.
- All "result" and "output" values: UPPERCASE, strip
  whitespace and punctuation.
- All mechanism type names: lowercase snake_case.

### STEP 3: Deduplication
Two trace entries are duplicates if, after normalization,
they would produce the same canonical 4-facet tuple.
Specifically:
- Same parse (same position + same text, ignoring whitespace)
- Same mechanism (same ordered list of types)
- Same execution (same steps with same indicator/fodder/result)
- Same output

For each set of duplicates, keep the FIRST occurrence
(preserving chronological order). For the removed entries,
add a comment: # DUPLICATE OF <line_number>

Also flag near-duplicates: entries that differ in only one
facet by a trivial variation (e.g., same parse/mechanism/
execution but output differs only in capitalization). Merge
these if the difference is purely formatting; keep both if
the difference is substantive.

### STEP 4: Consistency check
For each remaining entry, verify:
- If parse.text is set, it actually appears in the clue.
- If execution fodder.text is set, it appears in the clue.
- If mechanism is set but execution is None, that is fine
  (partial idea).
- Flag any entry where ALL facets are None (should not exist).

## OUTPUT
Produce the cleaned trace in the same Python format as the
input. Include a summary comment at the top:

# CLEANUP SUMMARY:
# - Entries before: <N>
# - Entries after:  <N>
# - Duplicates removed: <N>
# - Mechanisms remapped: <list>
# - Novel types retained: <list or "none">
# - Consistency warnings: <list or "none">
'''.strip()


PROMPT_C = r'''
NOVEL MECHANISM TYPES ENCOUNTERED:
{novel_types}

STANDARD TYPES FOR REFERENCE:
anagram, hidden_word, reversal, container, deletion,
charade, double_def, homophone, cryptic_def, &lit,
acrostic, substitution, letter_selection.

## YOUR TASK
For each novel type, decide:

1. REMAP: It is actually a standard type under a non-standard
   name. Output: {{"action": "remap", "from": "<novel>",
   "to": "<standard>"}}

2. DECOMPOSE: It is a compound of standard types that the
   coder failed to decompose. Output: {{"action": "decompose",
   "from": "<novel>", "to": ["<type1>", "<type2>"]}}

3. MERGE: Two or more novel types are actually the same thing.
   Output: {{"action": "merge", "types": ["<a>", "<b>"],
   "canonical": "<name>", "description": "<one-line>"}}

4. KEEP: It is genuinely novel and does not fit any standard
   type. Output: {{"action": "keep", "type": "<name>",
   "description": "<one-line>"}}

Output a JSON list of decisions.
'''.strip()


PROMPT_GOLD = r'''
CLUE: "{clue_text}" ({clue_enum})
CORRECT ANSWER: {answer}

SOLVER'S SOLUTION EXPLANATION:
{solution_text}

## YOUR TASK
Code the CORRECT SOLUTION ONLY into a single faceted idea dict.
This is for building a gold-standard reference, so be precise.

## OUTPUT FORMAT
Produce ONLY a single Python dict literal (no trace.append, no
tree_id, no parent_path — just the raw facets dict):

```python
gold = {{
    "parse":     <parse_value>,
    "mechanism": <mechanism_value>,
    "execution": <execution_value>,
    "output":    "{answer}",
}}
```

### FACET 1: parse
Identifies the definition portion of the clue.
  {{"position": "start", "text": "<substring>"}}
  {{"position": "end",   "text": "<substring>"}}
  {{"position": "whole"}}  # for &lit or cryptic_def
  {{"position": "other",  "text": "<substring>"}}  # rare mid-clue def

The "text" MUST be an exact substring of the clue (preserve case
and punctuation from the clue).

### FACET 2: mechanism
Ordered list of wordplay types in APPLICATION order (not clue order).
Standard types:
  anagram, hidden_word, reversal, container, deletion,
  charade, double_def, homophone, cryptic_def, &lit,
  acrostic, substitution, letter_selection

For compound mechanisms use a list: ["deletion", "anagram"]
For single mechanisms use a string: "hidden_word"

### FACET 3: execution
List of step dicts, one per mechanism step:
  [{{"indicator": {{"text": "<quoted from clue>"}},
    "fodder":    {{"text": "<quoted from clue>"}},
    "result":    "<UPPERCASE>"}}]

For deletions: add "delete": "<letters removed>".
For containers: use "outer"/"inner" instead of "fodder".
For acrostics: "fodder" = the words whose initials are taken.
All "text" fields must be EXACT substrings of the clue.
"result" must be UPPERCASE.

### FACET 4: output
Always "{answer}" for gold.

## RULES
1. Code ONLY the correct solution, not rejected alternatives.
2. Every facet must be populated (no None values).
3. Use standard mechanism types unless genuinely novel.
4. For compound clues with multiple mechanism steps, list ALL
   steps in execution and use a list for mechanism.
5. Keep text references as short as possible while still being
   exact clue substrings.
'''.strip()
