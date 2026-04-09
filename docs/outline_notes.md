# Abstract
Existing AI methods can be highly effective at solving well-specified problems. However, humans still surpass AI in handling novel problems, where the solver must simultaneously find a tractable formulation and a solution using that formulation. This work makes progress toward understanding what ingredients are currently missing from AI and proposes methods to close the gap. We first formalize this problem of simultaneously formulating and solving, which we label ``the creative problem-solving problem'', then use it to derive a simple, general-purpose prompting strategy aimed at one key part of the solving process. Using state of the art foundation models, we show that our prompting strategy outperforms related baselines on two challenging puzzle tasks chosen to target this aspect of problem solving, and at least matches performance of the "reasoning" versions of each model. Our formalism suggests good metaheuristics should be important to a solver's success and our analyses support this hypothesis, revealing specific failure modes in underperforming strategies. Finally, we suggest ways to build on this result through the lens of search metaheuristics.


# Introduction

Automated problem solving has an extensive history in cognitive science. Yet, the algorithms developed to date can still only reliably solve well-defined, well-characterized problems. By contrast, human problem-solvers frequently face problems that are related to but distinct from previous ones, and must simultaneously both define and solve them. New problems range from those that are relatively close to familiar problems (and thus more tractable) to those that are quite novel and challenging. The former are more common, such as a new game that is a slight variant on older ones, while the latter are rare and include major scientific breakthroughs. For convenience, I use ``creative problem solving'' (CPS) as an umbrella term, allowing that problems vary in their novelty and thus the amount of ``creativity'' they require.  Here, I formalize this problem in order to systematically study what is currently missing from automated problem-solving systems, what needs to be built to achieve human-level competence, and propose productive avenues toward that goal.

## Formalization
$$\argmin_{\theta \in \Theta,\, \Theta \in \Theta_0,} \sigma(y)$$
$$y_\theta = g(\theta)(x)$$

where:
\begin{itemize}
    \item $(x, \sigma)\sim P(\cdot)$ is a problem instance: $x$ is the set of knowns and $\sigma$ is a scoring function that evaluates candidate solutions. (In practice, $\sigma$ may be specified implicitly, e.g.\ as natural language descriptions of game rules or a performance criterion, and must be constructed by the solver.)
    \item $y$ is the candidate solution produced by program $g(\theta)$ given knowns $x$
    \item $g : \Theta \to \Pi$ maps a compact parameter space $\Theta$ to a program space $\Pi$, encoding prior knowledge
    \item $\theta \in \Theta \subseteq \Theta_0$ with $\dim(\Theta) \ll \dim(\Pi)$, and $\Theta_0$ is a larger library of concepts
    \item $(x,\sigma) \in S$, where $S$ has hierarchical structure, e.g.\ $(x, \sigma)$'s can be organized within a tree
\end{itemize}
The hierarchical structure of $S$ is defined through $\Theta$. Let $\theta^*_{(x,\sigma)} = \argmin_\theta \sigma(y_\theta)$. A set $S$ of problem instances with known valid solutions $\theta$ may be siblings under an implied parent if:

$$\text{siblings}(S) \iff \exists\, d \in \Theta : \forall\, (x_i, \sigma_i), (x_j, \sigma_j) \in S, \; \theta_{(x_i,\sigma_i)}.d = \theta_{(x_j,\sigma_j)}.d$$
That is, the solution parameters for all members of $S$ agree on at least one dimension. Two problems are closely related if most of the dimensions $d$ are shared (not just one). Intuitively, new problems should be easier to solve if a parent class can be identified whose members are closely related to the new problem.

## Choice of problem domains
I selected testing domains that strike a balance between having some degree of novelty for LLMs while still having a well-understood structure. More concretely, we target problems where LLMs are generally capable of sampling reasonable $\Theta$ (i.e. abstract problem dimensions) and applying $g(\theta)(x)$ (i.e. strong domain knowledge), but not necessarily adept at finding $\theta^*$ or revising its choice of $\Theta$ to reduce search costs. While active research problems from science provide the strongest validation, they are likely ill-suited to initial experimentation because they are too difficult for current models (inducing failure on either $\Theta$ or $g(\theta)(x)$) and time-consuming for an experimenter to collect. On the opposite end, there are myriad benchmarks of simpler, more controlled problems, but LLMs have mastered many of these already due to good coverage in their training sets, making them ``known'' to the model (finding $\theta^*$ is easy). Certain kinds of puzzle games provide a promising alternative that strikes a middle ground between these two, e.g. decipherment puzzles. The two examples I evaluate on are:
\begin{itemize}
    \item Cryptic crossword clues: These puzzles require the solver to guess the string operations to be applied to a natural language-style string, and are provided a set of criteria their solution should fulfill. Cryptic clues come with a set of fairly well-established conventions that make the search tractable for skilled solvers but leave open a large number of possibilities and room for "tricks".
    \item Rosetta Stone-style translation problems with constructed languages: These are few-shot learning puzzles where each instance provides a list of $k$ English-conlang pairs and asks the solver to complete the last translation. Conlangs are drawn from a meta-grammar, but puzzles are carefully designed so that all necessary information is provided, assuming complete knowledge of the meta-grammar.
\end{itemize}
We can map the CPS formalism onto decipherment puzzles by saying that solving a puzzle instance is both defining its sub-type within the larger (parent) class of puzzles and solving it given that definition. For instance, while cryptic clues may use any combination of word-play mechanisms (anagrams, substitution, selection, etc.), a sub-type could be defined for clues that use one anagram and one selection operation. A solver could then generate a solution on this assumption. While both of these types of problem appear in BigBench, a core benchmark for language models, and thus are in the LLM training sets, it is easy to generate new puzzles that models cannot have seen before and to vary their difficulty. E.g., Rosetta Stone puzzles are programmatically generated and cryptic clues are popular with online communities whose members continually generate new puzzles. In addition, while it may be possible to design specialized algorithms to solve each of these problems [cite cryptic LLM paper], the relevant point is that any given LLM we evaluate does not already know this algorithm.

## Meta-heuristics
-- In experiments here, we focus on search over \theta, and less so on search over \Theta (which can pretty readily be elicited from the LLM)
-- The search over \theta (but also over \Theta) is a combinatorial optimization problem (in the domains considered here), which suggests the need for good metaheuristics (https://www.iiia.csic.es/~christian.blum/downloads/blum_roli_2003.pdf)
-- It is hard to directly measure what metaheuristics are used by LLMs. We would like to understand their search strategies in a meaningful abstract space of domain-specific representations. But conceptual "actions" by the LLM can be viewed at multiple levels of abstraction and internal to the network vs external. Thoughts happen inside the network and could be viewed at a granular or abstract level. Output tokens can be viewed as actions, and examined at the individual level or in terms of the abstract ideas they express.
-- Here, we take the approach of coding outputs in terms of domain-specific abstractions and then tracking idea generation in this space. We express the ideation process as a series of tree edit operations over a potentially growing forest. New ideas are either developments of previous ideas and assigned as children, or form roots of new trees.
-- We can form strategy profiles by inspecting statistical differences between prompting strategies in this idea-space.
-- We measure how a solver allocates their attention across the solution space and hypothesize that weaker strategies will show signs of misallocating their attention
-- In the domain of cryptic crosswords, we find that weak strategies misallocated their attention by under-exploring clue parses, and to a lesser degree indicator mechanisms, compared to the strongest strategies.

## Implications

## Related works
-- Cognitive foundations for reasoning (https://arxiv.org/pdf/2511.16660). Differs from present work mostly in that the cognitive skills listed are largely descriptive rather than proscriptive. Many of them are needed in order for solvers to succeed in the present puzzle tasks, but here we partly prescribe how to implement them in problems where the solution steps cannot be immediately inferred.

# Methods

## Tasks
-- Minute Cryptic puzzles from Sept. 2025 to March 2026, par-1 and par-2 subsets
-- BigBench Rosetta task (easy variant made by customizing original generating script)
## Base LLMs
-- deepseek-v3.2 (chat/reasoner)
-- claude-opus-4.6 (instant/max-thinking)
## Prompting strategies

{Fill in details for me based on code base}

# Results

## Performance
-- Accuracy bar plots
---- generate-vars has best overall performance but very similar to reasoning versions of each LLM
---- self-discover generally second place. makes sense, because it has a long list of strategies to seed ideas and use a json to impose structure
---- step-back is about as bad as keep-thinking. Not surprising, given it's a subset of the strategies included in self-discover 
-- Line plots (solution_iterations.png)
---- asymptoting with most strategies. perhaps not yet with generate-vars
(TODO: Combine plots to include both domains side-by-side, including both minute-cryptic easy and medium?)

## Explaining performance differences
-- (Reduced) radar chart with entropies and n-idea and maybe jaccard-nn-mean

Claim: \Theta provides useful baseline scaffolding for good search metaheuristics in CPS since it aligns the thinking to specific axes

- Q: What accounts for performance differences?
    - A: Different search strategies (meta-heuristics)
        1. Domain-knowledge alignment: Better models produce greater quantity of useful ideas (as measured by how often they can be coded into the gold standard representation)
            1. Evidence: n_ideas variable
        2. Transition structure: Better models have higher entropy on prerequisite facets (parse and mechanism), which allows more fruitful generation on the remaining facets (execution and output).
            1. There was some room for variability in strategy:
                1. generate-vars front-loaded on parse, allowing it to have high entropy on execution, while not wasting its idea budget on output guesses
                2. self-discover tended to work backward from guesses (high output entropy, lower relatively low parse entropy, mechanism entropy closer to generate-vars, execution entropy same as generate-vars). It also generated lots of guesses, which resulted in a higher idea count than generate-vars
                3. step-back and keep-thinking exhibit idiosyncratic bias: They both lean into step-by-step style thinking, which often results in clue-parse as step 1. The initial choice tends to get anchored on and not revisited due to LLM bias
    - Implications:
        - Why generate-vars works:
            - Forces working memory to contain a useful problem definition structure that supports a more thorough search via a sorted cartesian product
            - More fine-grained meta-control could bring additional benefits
        - Further improvements:
            - Problem-structure-aware variants of generate-vars (e.g. additional instructions to weight prerequisites more heavily or generate meta-control thoughts assessing search strategy and propose shift)
        - Challenges applying intensification-diversification framework:
            - Problem dimensions have dependencies between them, so it’s hard to indirectly assess I vs D
            - Need direct measurement of value function. Either the value estimator or the meta-heuristic is bad, or both. However, tricky to nail down value estimator in this space since the action space is ill-defined: maybe it includes “choose which facet to vary next”, “generate new possibility under facet f”, “retrieve related examples from memory”, etc. Also, the state-space is ill-defined: LLM may not have set up Theta properly, leading to poor idea generation even if value function is good relative to choice of Theta.


{Fill in technical details for me based on results outputs}


# Discussion

# Conclusion

# References

# Appendices