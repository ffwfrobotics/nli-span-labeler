"""
The E13 label set (requirements §1.4) and per-reason defaults.

Order matters: it sets the keyboard positions 1..9, 0 (§6.1), so the two
candidates sit at fixed keys 5 and 0.

Owner decisions, 2026-10-05 (requirements §11 Q3, Q7, Q8):
- These ten keys are final. ``out_of_scope`` is dropped: it describes the model,
  not the item, and the API's ``model_uncertainty`` covers it.
- ``stale_state`` is judged against the question's own time reference: the item's
  ``asof`` date, which defaults to today. Never against world knowledge.
- ``false_premise`` is judged against the state only.
- ``answerable`` stays a separate flag, not a reason.
"""

REASONS = (
    "unrelated",
    "not_enough_info",
    "conflicting_evidence",
    "non_factual_support",
    "stale_state",
    "ambiguous",
    "underspecified",
    "false_premise",
    "no_option_fits",
    "subjective",
)

CANDIDATES = frozenset({"stale_state", "subjective"})
ESTABLISHED = tuple(r for r in REASONS if r not in CANDIDATES)

DEFINITIONS = {
    "unrelated": "The state is not about what the question asks (topicality, not answerability).",
    "not_enough_info": "On topic, but the state doesn't settle the question.",
    "conflicting_evidence": "The state both supports and refutes the same option. Tick it explicitly, and mark "
                            "at least one support and one refute span on that option.",
    "non_factual_support": "The only support is hedged, attributed, hypothetical or quoted. "
                           "Negation does not count: a negated fact is a refutation.",
    "stale_state": "The question asks about a time (the item's as-of date, by default today), and the state's "
                   "information is dated or time-sensitive in a way that may no longer hold then. Judge whether "
                   "it could have changed, not whether it did; don't use world knowledge.",
    "ambiguous": "The question has several plausible readings that would get different answers.",
    "underspecified": "The question lacks a parameter it needs (whose, when, which unit, compared with what).",
    "false_premise": "The question presupposes something the state contradicts (judged against the state only, "
                     "not world knowledge).",
    "no_option_fits": "The state settles the matter, but none of the offered options matches.",
    "subjective": "The answer depends on taste or opinion rather than on the state.",
}

# FR-19 proposed defaults: required | optional | none
DEFAULT_SPAN_POLICY = {
    "conflicting_evidence": "required",
    "non_factual_support": "required",
    "stale_state": "required",
    "false_premise": "required",
    "unrelated": "none",
    **{r: "optional" for r in ("not_enough_info", "ambiguous", "underspecified", "no_option_fits", "subjective")},
}

# Span rules no batch policy or override can relax (owner decisions, 2026-10-05):
# conflicting_evidence needs support + refute on one option; stale_state needs the
# dated or time-sensitive phrase.
HARD_SPAN_RULES = frozenset({"conflicting_evidence", "stale_state"})

# FR-14: a batch may require a note when one of these is checked.
NOTE_PROMPTING = frozenset({"ambiguous", "underspecified", "subjective"})

SPAN_ROLES = ("support", "refute", "unsupported", "framing")
SKIP_CODES = ("cannot_judge", "broken_item", "offensive", "too_long", "other")
QUESTION_TYPES = ("noul", "choice", "score")
