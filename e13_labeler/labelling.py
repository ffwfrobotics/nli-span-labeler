"""
The `reasons` task: what a labeler sees, and validation of what they submit
(requirements §4.2, §5.3, §5.4).

Kept free of FastAPI and SQL so the rules can be tested directly.
"""

import json
from dataclasses import dataclass, field
from typing import Optional

from .reasons import DEFAULT_SPAN_POLICY, HARD_SPAN_RULES, NOTE_PROMPTING, REASONS, SPAN_ROLES

NOTE_MAX_CHARS = 2000

# Only these question keys reach the labeler (FR-12). Anything else the importer
# kept (gold, generator targets, ...) lives in other columns.
QUESTION_KEYS = ("type", "instructions", "criteria")


class SubmissionError(ValueError):
    """A submission the API must reject; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]):
        super().__init__("; ".join(problems))
        self.problems = problems


class PolicyViolation(SubmissionError):
    """Span policy unmet (FR-19). Resubmitting with policy_override saves anyway."""


# ============================================================================
# Questions and options
# ============================================================================

def blind_question(question: dict) -> dict:
    return {k: question[k] for k in QUESTION_KEYS if k in question}


def option_keys(question: dict) -> list[str]:
    """
    The option names spans refer to (FR-16): choice keys in order, score levels
    "0".."n-1", and "true"/"false" for noul.
    """
    qtype = question["type"]
    if qtype == "choice":
        return [str(k) for k in question["criteria"]]
    if qtype == "score":
        return [str(i) for i in range(len(question["criteria"]))]
    return ["true", "false"]


def option_description(question: dict, option: str) -> Optional[str]:
    """The string option-side span offsets index into (rule 9), or None if it has none."""
    criteria = question.get("criteria")
    if question["type"] == "choice":
        value = criteria.get(option)
    elif question["type"] == "score":
        value = criteria[int(option)]
    else:
        value = (criteria or {}).get(option)
    return value if isinstance(value, str) else None


# ============================================================================
# JSON pointers (RFC 6901)
# ============================================================================

def resolve_pointer(document, pointer: str):
    """Resolve an RFC 6901 pointer. Returns (parent, last token, value); raises KeyError."""
    if pointer == "":
        return None, None, document
    if not pointer.startswith("/"):
        raise KeyError(f"pointer must start with '/': {pointer!r}")
    parent, token, value = None, None, document
    for raw in pointer[1:].split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        parent = value
        if isinstance(value, dict):
            if token not in value:
                raise KeyError(f"no key {token!r}")
            value = value[token]
        elif isinstance(value, list):
            if not token.isdigit() or int(token) >= len(value) or (token != "0" and token.startswith("0")):
                raise KeyError(f"bad index {token!r}")
            value = value[int(token)]
        else:
            raise KeyError(f"cannot descend into a scalar at {token!r}")
    return parent, token, value


def scalar_text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value)


# ============================================================================
# Submissions
# ============================================================================

@dataclass
class Span:
    side: str
    role: str
    text: str
    option: Optional[str] = None
    pointer: Optional[str] = None
    start: Optional[int] = None
    end: Optional[int] = None
    reasons: list = field(default_factory=list)


@dataclass
class Submission:
    answerable: bool
    reasons: list           # checked reasons
    note: Optional[str]
    spans: list             # [Span]
    policy_override: bool
    active_ms: Optional[int]


def validate_span(span: Span, index: int, state: str, state_format: str, question: dict,
                  checked: set) -> list[str]:
    """FR-15 to FR-17 for one span. Returns problems (empty if valid)."""
    where = f"span {index + 1}"
    problems = []
    if span.role not in SPAN_ROLES:
        return [f"{where}: unknown role {span.role!r}"]
    if span.side not in ("state", "option"):
        return [f"{where}: side must be state or option"]
    if span.role == "unsupported" and span.side != "option":
        problems.append(f"{where}: role unsupported is for option-side spans only")
    if span.role == "framing" and span.side != "state":
        problems.append(f"{where}: role framing is for state-side spans only")

    options = option_keys(question)
    if span.option is not None and span.option not in options:
        problems.append(f"{where}: unknown option {span.option!r}")
    unknown = [r for r in span.reasons if r not in checked]
    if unknown:
        problems.append(f"{where}: linked to unchecked reason(s) {', '.join(unknown)}")
    if not span.text:
        problems.append(f"{where}: empty text")
    if problems:
        return problems

    # FR-17: the stored text must equal the slice it claims to be.
    has_offsets = span.start is not None or span.end is not None
    if has_offsets and (span.start is None or span.end is None or not 0 <= span.start < span.end):
        return [f"{where}: start and end must both be set, with 0 <= start < end"]

    if span.side == "option":
        if span.option is None:
            return [f"{where}: option-side spans must name their option"]
        if span.pointer is not None:
            return [f"{where}: option-side spans take offsets, not a pointer"]
        target = option_description(question, span.option)
        if target is None:
            return [f"{where}: option {span.option!r} has no description to select from"]
        if not has_offsets:
            return [f"{where}: option-side spans need start and end"]
    elif state_format == "text":
        if span.pointer is not None:
            return [f"{where}: text states take offsets, not a pointer"]
        if not has_offsets:
            return [f"{where}: start and end are required"]
        target = state
    else:
        if span.pointer is None:
            return [f"{where}: JSON states need a pointer"]
        try:
            _parent, token, value = resolve_pointer(json.loads(state), span.pointer)
        except KeyError as e:
            return [f"{where}: pointer {span.pointer!r} does not resolve ({e})"]
        if not has_offsets:
            # The pointer alone marks a whole scalar, or a key.
            if isinstance(value, (dict, list)):
                if span.text != token:
                    return [f"{where}: a bare pointer to an object or array marks its key, {token!r}"]
                return []
            if span.text not in (scalar_text(value), token):
                return [f"{where}: text does not match the value at {span.pointer}"]
            return []
        if not isinstance(value, str):
            return [f"{where}: offsets need a string value at {span.pointer}"]
        target = value

    if span.end > len(target) or target[span.start:span.end] != span.text:
        return [f"{where}: text {span.text!r} is not the slice [{span.start}:{span.end}]"]
    return []


def _span_rule(reason: str, linked: list) -> Optional[str]:
    """The span rule for one checked reason, or None if it is met."""
    if reason == "conflicting_evidence":
        options = {s.option for s in linked if s.role == "support"} & {s.option for s in linked if s.role == "refute"}
        return None if options else "conflicting_evidence needs a support and a refute span on the same option"
    if reason == "non_factual_support":
        roles = {s.role for s in linked}
        return None if {"framing", "support"} <= roles else "non_factual_support needs a framing span and a support span"
    if reason == "stale_state":
        return None if linked else "stale_state needs a span on the dated or time-sensitive phrase"
    return None if linked else f"{reason} needs at least one span"


def hard_rule_problems(submission: Submission) -> list[str]:
    """
    Span rules that neither a batch's span policy nor Shift+Enter can relax (owner
    decisions, 2026-10-05): conflicting_evidence needs a support and a refute span
    on one option; stale_state needs the dated or time-sensitive phrase.
    """
    problems = []
    for reason in submission.reasons:
        if reason in HARD_SPAN_RULES:
            problem = _span_rule(reason, [s for s in submission.spans if reason in s.reasons])
            if problem:
                problems.append(problem)
    return problems


def policy_problems(submission: Submission, span_policy: dict) -> list[str]:
    """FR-19: the batch's required spans for checked reasons (overridable with Shift+Enter)."""
    problems = []
    for reason in submission.reasons:
        if reason in HARD_SPAN_RULES:
            continue  # checked by hard_rule_problems
        if span_policy.get(reason, DEFAULT_SPAN_POLICY.get(reason, "optional")) != "required":
            continue
        problem = _span_rule(reason, [s for s in submission.spans if reason in s.reasons])
        if problem:
            problems.append(problem)
    return problems


def validate_submission(submission: Submission, *, state: str, state_format: str, question: dict,
                        reason_set: list, span_policy: dict, require_note: bool) -> None:
    """Raise SubmissionError (or PolicyViolation) unless the submission is valid."""
    problems = []
    checked = list(dict.fromkeys(submission.reasons))
    submission.reasons = checked

    # FR-13: answerable is exclusive, and something must be chosen.
    if submission.answerable and checked:
        problems.append("answerable excludes every reason")
    if not submission.answerable and not checked:
        problems.append("choose answerable or at least one reason")
    not_asked = [r for r in checked if r not in reason_set]
    if not_asked:
        problems.append(f"reason(s) not in this batch: {', '.join(not_asked)}")

    # FR-14: note
    note = (submission.note or "").strip() or None
    submission.note = note
    if note and len(note) > NOTE_MAX_CHARS:
        problems.append(f"note is longer than {NOTE_MAX_CHARS} characters")
    prompting = sorted(set(checked) & NOTE_PROMPTING)
    if require_note and prompting and not note:
        problems.append(f"a note is required when {', '.join(prompting)} is checked")

    if submission.active_ms is not None and submission.active_ms < 0:
        problems.append("active_ms must be >= 0")

    for i, span in enumerate(submission.spans):
        problems.extend(validate_span(span, i, state, state_format, question, set(checked)))
    if problems:
        raise SubmissionError(problems)
    hard = hard_rule_problems(submission)
    if hard:
        raise SubmissionError(hard)

    unmet = policy_problems(submission, span_policy)
    if unmet and not submission.policy_override:
        raise PolicyViolation(unmet)
    if not unmet:
        submission.policy_override = False  # only record an override that overrode something


def reasons_json(checked: list, reason_set: list) -> dict:
    """§5.1: True = present, False = asked and absent, None = not in the batch's reason set."""
    return {r: (r in checked if r in reason_set else None) for r in REASONS}
