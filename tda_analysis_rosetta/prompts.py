"""
LLM prompt templates for the TDA creativity pipeline — Rosetta variant.

Prompt A: Initial coding (solver output → tree trace)
Prompt B: Deduplication & normalization
Prompt C: Novel-type review
"""

# ─────────────────────────────────────────────────────────────────────────────
# Canonical facet schema — 1 lexicon, 4 word-orders, 8 (POS × feature)
# morphology slots, 1 relative-clause bundle. Mirrors every grammar
# dimension picked per-puzzle by linguistics-puzzle-generator-task.py.
# (The solver's translation guess is NOT a facet — facets describe the
# inferred grammar of the conlang, the translation is the downstream
# application of that grammar.)
# ─────────────────────────────────────────────────────────────────────────────

FACET_KEYS = (
    # A. Lexicon
    "lexicon",
    # B. Word order
    "phrase_order",
    "noun_phrase_order",
    "noun_marking_order",
    "verb_marking_order",
    # C. Morphology — one slot per (POS × feature). Each value is a dict
    # of {<feature-value>: <affix-or-null>, "marker_type": SUFFIX|PREFIX|
    # PARTICLE_AFTER}. A null affix means the value is a zero morpheme
    # (e.g. nom_is_no_op, singular_verb_is_no_op, present_tense_is_no_op).
    "noun_case",
    "noun_plurality",
    "det_case",
    "det_plurality",
    "adj_case",
    "adj_plurality",
    "verb_tense",
    "verb_plurality",
    # D. Relative clauses
    "relative_clause",
)


PROMPT_A = r'''
PUZZLE (5 example sentence pairs in another language ↔ English, then a
partial 6th sentence to translate):
{clue_text}

SOLVER OUTPUT:
{llm_outputs}

## YOUR TASK
The puzzle above is a Rosetta-Stone-style linguistics problem. Each
puzzle is its own conlang: a freshly drawn grammar + lexicon. The five
example sentence pairs are evidence; the solver must induce that
grammar and apply it to translate the sixth sentence.

Analyze the solver output and produce an ordered trace of ideas as
Python code. Each trace entry records a hypothesis the solver proposed
about the conlang's grammar, where facets are independent dimensions of the grammar whose values are to be induced.

## OUTPUT FORMAT
Produce ONLY Python code in this format:

```python
# Key phrase: "<quote from solver output>"
trace.append(("<tree_id>", <parent_path>, {{
    # any subset of the 14 facets below — omit (or use None for) facets
    # the solver hasn't committed to in this idea
    "lexicon":            <dict or None>,
    "phrase_order":       <list or None>,
    "noun_phrase_order":  <list or None>,
    "noun_marking_order": <list or None>,
    "verb_marking_order": <list or None>,
    "noun_case":          <dict or None>,
    "noun_plurality":     <dict or None>,
    "det_case":           <dict or None>,
    "det_plurality":      <dict or None>,
    "adj_case":           <dict or None>,
    "adj_plurality":      <dict or None>,
    "verb_tense":         <dict or None>,
    "verb_plurality":     <dict or None>,
    "relative_clause":    <dict or None>,
}}))
```

- tree_id: "tree1", "tree2", … Start a new tree when the solver opens a
  fresh, INDEPENDENT line of reasoning (e.g. abandons one phrase-order
  hypothesis and adopts another). Reuse a tree_id when continuing.
- parent_path: list of child indices from root, e.g. [] root, [0] first
  child of first root, [0,1] second child of that node.

### STANDARD FACETS
The 14 facets below are what the generator we have on hand uses, so
they cover the typical Rosetta puzzle. ALWAYS try to express a solver
commitment with one of these first. Linguistic features outside this
list are possible — if the solver describes one, see "NOVEL FEATURES"
below.

`lexicon`
    Dict mapping a foreign word (as it appears in the puzzle, stripped
    of any affix) to its English gloss. Partial dicts are fine — each
    entry is a separate solver commitment. Example:
        {{"dima": "cat", "fere": "the"}}

`phrase_order`
    Ordered list naming the main-clause word order. Drawn from:
    "subject", "verb", "object". Example: ["subject", "verb", "object"]

`noun_phrase_order`
    Ordered list naming the order of constituents inside a noun phrase.
    Drawn from: "det", "adj", "noun". Example: ["det", "adj", "noun"]

`noun_marking_order` / `verb_marking_order`
    Which feature attaches closer to the stem. List of strings.
    `noun_marking_order` is drawn from ["case", "plurality"];
    `verb_marking_order` is drawn from ["tense", "plurality"].
    Order = [innermost, outermost].

`noun_case` / `det_case` / `adj_case`
    Dict describing the case-marking paradigm for that part of speech:
        {{"nom": "<affix>" or null,   # null = zero morpheme
          "acc": "<affix>",
          "marker_type": "SUFFIX" | "PREFIX" | "PARTICLE_AFTER"}}
    Omit the whole facet if the solver believes that POS carries no
    case marking at all. Partial entries (only `nom` or only `acc`) are
    fine — each is its own commitment.

`noun_plurality` / `det_plurality` / `adj_plurality`
    Same shape with keys "sg", "pl" instead of "nom", "acc". A null
    `sg` value means singular is unmarked (zero morpheme).

`verb_tense`
    Dict with keys from "past", "present", "future". Any of them may
    be null (zero morpheme). Plus `marker_type`. Example:
        {{"past": "vi", "present": null, "future": "ku",
          "marker_type": "SUFFIX"}}

`verb_plurality`
    Dict with keys "sg", "pl" + marker_type. Omit the facet entirely if
    the solver believes the verb does NOT agree with subject number.

`relative_clause`
    Dict:
        {{"marker": "<word>",                   # the relativizer word
          "order": "rel_first" | "rel_last"}}   # rel clause pre/post head

### NOVEL FEATURES
If the solver describes a property of the conlang that does not fit
ANY of the 14 standard facets, introduce a new snake_case facet key
and prefix it with ``novel_``, with a brief comment explaining what
it means.
Examples:
    novel_vowel_harmony  # affixes harmonize with stem vowel
    novel_noun_class     # nouns belong to gender/class groups
    novel_evidentiality  # verbs mark how the speaker knows

### KEY PHRASE COMMENT
Each `trace.append` is preceded by a `# Key phrase:` comment containing
the key part of the output where the solver expressed this hypothesis.

## RULES
1. Walk the solver output in order; emit one trace entry per discrete
   hypothesis as it appears. Preserve chronological order.
2. If the solver revisits an earlier hypothesis (refining it), reuse
   the tree_id and extend from the relevant parent_path. If the solver
   abandons a hypothesis and tries a different value, start a new tree.
3. Populate ONLY facets the solver has actually committed to in that
   idea. Leave everything else None / omitted.
4. If the solver explicitly REJECTS a hypothesis, still record it and
   annotate with ``# REJECTED``.
5. One solver statement that yields multiple commitments at once is
   ONE trace entry with multiple non-None facets.

## CANONICAL VALUE FORMS
These value conventions are PRESCRIBED — facet-value entropy is
estimated across strategies and across coded puzzles, so identical-
meaning values that differ in form (e.g. ``"co"`` vs ``"-co"``) would
be counted as distinct ideas. Follow the conventions exactly, both
within a single trace AND across every trace.

- Affix glyphs (every value inside `*_case`, `*_plurality`,
  `verb_tense`): the raw character sequence as it appears in the
  puzzle, lowercased, **no leading/trailing hyphen, no whitespace**.
  Example: write ``"co"``, NOT ``"-co"`` or ``"co-"``. Position
  information is already carried by `marker_type`.
- `marker_type` values: exactly one of ``"SUFFIX"``, ``"PREFIX"``,
  ``"PARTICLE_AFTER"`` (uppercase, no surrounding whitespace).
- `lexicon` keys (foreign word): lowercased, stripped of every affix
  the solver has identified, the bare stem only.
- `lexicon` values (English gloss): lowercased, singular base form
  (cat / dog / monkey — NOT "cats", "the cat", "Cat", etc.).
- `phrase_order` / `noun_phrase_order` values: lowercased role names
  from the closed set ``{"subject","verb","object"}`` or
  ``{"det","adj","noun"}``. No abbreviations (no "subj", "obj").
- `noun_marking_order` / `verb_marking_order` values: lowercased
  feature names from ``{"case","plurality"}`` or
  ``{"tense","plurality"}``. Order = [innermost first, outermost
  last].
- `relative_clause.marker`: the foreign word verbatim from the
  puzzle, lowercased, stripped of surrounding whitespace.
- `relative_clause.order`: exactly ``"rel_first"`` or
  ``"rel_last"``.
'''.strip()


PROMPT_B = r'''
PUZZLE:
{clue_text}

RAW TRACE:
{raw_trace}

## YOUR TASK
Clean the trace by applying these operations IN ORDER:

### STEP 1: Facet-key check
The standard facet keys are:
    lexicon, phrase_order, noun_phrase_order, noun_marking_order,
    verb_marking_order, noun_case, noun_plurality, det_case,
    det_plurality, adj_case, adj_plurality, verb_tense, verb_plurality,
    relative_clause.

If a trace entry uses a non-standard key:
- If it is a clear synonym of a standard key (e.g. ``"morphology"``
  for the per-POS facets, ``"word_order"`` for ``phrase_order``),
  remap it now.
- If it describes a real linguistic feature the standard schema does
  not cover (e.g. vowel harmony, tone, noun class), KEEP it but rename
  it to ``novel_<snake_case_name>`` and add a brief one-line comment.
  The batch novel-type review (PROMPT_C) will decide what to do with
  it across the dataset.

### STEP 2: Value normalization
Force every facet value into the canonical form below. These forms are
prescribed GLOBALLY (the entropy estimator runs across solver
strategies, so identical-meaning values that differ in form would be
counted as distinct — within-trace consistency is not enough).

- Affix glyphs in `*_case`, `*_plurality`, `verb_tense`: lowercased,
  **no leading/trailing hyphen, no whitespace**. Rewrite ``"-co"`` and
  ``"co-"`` and ``" co "`` all as ``"co"``. (Position info already
  lives in `marker_type`.)
- `marker_type` values: exactly one of ``"SUFFIX"``, ``"PREFIX"``,
  ``"PARTICLE_AFTER"`` (uppercase, no whitespace).
- Null affix values stay as None (do NOT delete the key — a
  present-but-null value is itself a commitment to a zero morpheme).
- `lexicon` keys: lowercased, stripped of every affix the solver had
  identified, bare stem only.
- `lexicon` values: lowercased English base form (e.g. ``cat``, not
  ``Cat``, ``cats``, or ``the cat``).
- `phrase_order`, `noun_phrase_order`, `noun_marking_order`,
  `verb_marking_order`: lowercased role/feature names spelled exactly
  ``subject``/``verb``/``object``, ``det``/``adj``/``noun``,
  ``case``/``plurality``, ``tense``/``plurality``. No abbreviations.
- `relative_clause.marker`: foreign-word string, lowercased,
  whitespace-stripped.
- `relative_clause.order`: exactly ``"rel_first"`` or ``"rel_last"``.

### STEP 3: Deduplication
Two trace entries are duplicates if, after normalization, they produce
the same canonical mapping of (facet_key → value). For each duplicate
set keep the FIRST occurrence (preserves chronological order);
annotate removed ones with ``# DUPLICATE OF <line_number>``.

### STEP 4: Consistency check
Flag entries where:
- The facets dict has no non-null values (should not exist).
- A `marker_type` value isn't one of the three allowed strings.
- `phrase_order` / `noun_phrase_order` lists contain duplicate or
  off-vocabulary names.
- Any other inconsistencies with the above instructions.

## OUTPUT
Cleaned trace in the same Python format. Prepend a summary comment:

# CLEANUP SUMMARY:
# - Entries before: <N>
# - Entries after:  <N>
# - Duplicates removed: <N>
# - Off-schema entries dropped: <N>
# - Consistency warnings: <list or "none">
'''.strip()


PROMPT_C = r'''
NOVEL FACET KEYS ENCOUNTERED:
{novel_types}

STANDARD FACET KEYS FOR REFERENCE:
lexicon, phrase_order, noun_phrase_order, noun_marking_order,
verb_marking_order, noun_case, noun_plurality, det_case, det_plurality,
adj_case, adj_plurality, verb_tense, verb_plurality, relative_clause.

## YOUR TASK
A ``novel`` facet key is one the coder invented (typically prefixed
``novel_``) because the solver described a property of the conlang
that did not fit any standard key — e.g. vowel harmony, tone, noun
class, reduplication, evidentiality. Most are coder errors that should
be remapped or decomposed; a few are genuine linguistic phenomena the
generator does not model and should be kept as new facets.

For each novel type, decide:

1. REMAP: It is actually a standard facet key under a non-standard
   name. Output: {{"action": "remap", "from": "<novel>",
   "to": "<standard>"}}

2. DECOMPOSE: It bundles multiple standard facet commitments that the
   coder failed to separate. Output: {{"action": "decompose",
   "from": "<novel>", "to": ["<key1>", "<key2>"]}}

3. MERGE: Two or more novel facet keys are actually the same thing.
   Output: {{"action": "merge", "types": ["<a>", "<b>"],
   "canonical": "<name>", "description": "<one-line>"}}

4. KEEP: It is genuinely novel and does not fit any standard facet
   key. Output: {{"action": "keep", "type": "<name>",
   "description": "<one-line>"}}

Output a JSON list of decisions.
'''.strip()


PROMPT_GOLD = r'''
PUZZLE:
{clue_text}
CORRECT TRANSLATION: {answer}

GROUND-TRUTH GRAMMAR (per-puzzle invariants):
{solution_text}

## YOUR TASK
Code the puzzle's CORRECT grammar into a single 14-facet dict. This is
a gold-standard reference, so fill in every facet the puzzle actually
uses (use None only for facets the puzzle's conlang genuinely lacks).

NOTE: Rosetta gold extraction is not currently wired into the main
pipeline — the BIG-bench task.json's exact generator state is lost, so
per-puzzle ground truth cannot be recovered for the existing CSV.
'''.strip()
