"""Human-reviewed steering: analysts propose structure; code owns every number."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from numbers import Real
from typing import Callable, Mapping, Protocol, Sequence

from .context import ContextPolicy
from .dynamic_elements import (CURRENT_DATETIME_ELEMENT, CURRENT_DATETIME_QUESTION_TYPE,
                               optimizer_dynamic_element_instruction)
from .feedback import Element, Scorecard
from .head import LearnedHead
from .run_ledger import FeatureActivity, FeatureDefinition


class Analyst(Protocol):
    def __call__(self, briefing: "AnalystBriefing") -> Mapping[str, object]: ...


class AnalystFactory(Protocol):
    """Build an analyst only after it has received the installed fake client."""

    def __call__(self, manager: "ScriptedMockManager") -> Analyst: ...


class ScriptedMockManager:
    """The sole offline analyst client; install it before a factory can run."""

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.installed = False

    def install(self) -> None:
        self.installed = True

    def build_analyst(self, factory: AnalystFactory) -> Analyst:
        if not self.installed:
            raise RuntimeError("scripted analyst mock was not installed before analyst construction")
        analyst = factory(self)
        if not callable(analyst):
            raise ValueError("scripted analyst factory must return a parser")
        return analyst

    def next_reply(self) -> str:
        if not self.installed or not self._replies:
            raise RuntimeError("scripted analyst mock was not installed before parsing")
        return self._replies.pop(0)


@dataclass(frozen=True)
class AnalystBriefing:
    """Safe analyst input: developer identifiers only, never scoreboard labels/text."""

    developer_ids: tuple[str, ...]
    developer_hashes: tuple[str, ...]
    scorecard_fingerprint: str
    policy_fingerprint: str
    dynamic_element_instruction: str = optimizer_dynamic_element_instruction()


@dataclass(frozen=True)
class SteeringProposal:
    add_element: Element | None = None
    context_policy_name: str | None = None
    add_programmatic_element: str | None = None


@dataclass(frozen=True)
class SteeringHistory:
    proposal_fingerprint: str
    decision: str  # rejected, fit-failed, not-better-on-development, promoted
    parent_scorecard_fingerprint: str
    candidate_scorecard_fingerprint: str | None
    reason: str | None = None


@dataclass(frozen=True)
class SteeringOutcome:
    scorecard: Scorecard
    policy: ContextPolicy
    accepted: bool
    promoted: bool
    numerical_refit: LearnedHead | None
    proposal: SteeringProposal
    history: tuple[SteeringHistory, ...]


def run_steering_round(
    scorecard: Scorecard,
    policy: ContextPolicy,
    *,
    analyst_factory: AnalystFactory,
    mock_manager: ScriptedMockManager,
    developer_ids: Sequence[str],
    developer_hashes: Sequence[str],
    protected_ids: Sequence[str],
    protected_hashes: Sequence[str],
    human_accept: Callable[[SteeringProposal], bool],
    development_objective: Callable[[Scorecard, ContextPolicy, LearnedHead], float],
    incumbent_development_objective: float,
    numerical_fitter: Callable[[Scorecard, ContextPolicy, tuple[str, ...]], LearnedHead],
    allowed_policies: Mapping[str, ContextPolicy] | None = None,
) -> SteeringOutcome:
    """Fit and evaluate a complete policy-bound child before it can be promoted.

    The analyst may propose one structural change only. The code derives the
    declared features from that child scorecard, fits a :class:`LearnedHead`,
    proves its policy and scorecard lineage, and only then gives it to the
    development objective. Scoreboard identifiers and hashes never cross the
    briefing boundary.
    """
    allowed_policies = {} if allowed_policies is None else allowed_policies
    _guard_initial_lineage(scorecard, policy)
    incumbent = _finite_objective("incumbent development objective", incumbent_development_objective)
    _guard_protected(developer_ids, developer_hashes, protected_ids, protected_hashes)
    briefing = AnalystBriefing(tuple(developer_ids), tuple(developer_hashes), scorecard.fingerprint, policy.fingerprint)
    # A factory receives the mock only after installation, so scripted tests do
    # not create a provider-backed analyst while parsing the proposal.
    mock_manager.install()
    analyst = mock_manager.build_analyst(analyst_factory)
    raw = analyst(briefing)
    proposal = _parse_proposal(raw, allowed_policies)
    proposal_fingerprint = _proposal_fingerprint(raw)
    accepted = human_accept(proposal)
    if type(accepted) is not bool:
        raise ValueError("human review must return an exact boolean")
    if not accepted:
        return _outcome(scorecard, policy, scorecard.fingerprint, False, False, None, proposal, proposal_fingerprint,
                        "rejected", None, None)

    candidate_policy = allowed_policies.get(proposal.context_policy_name, policy)
    candidate = _apply(scorecard, proposal, candidate_policy)
    declared_features = _declared_features(candidate)
    try:
        refit = numerical_fitter(candidate, candidate_policy, declared_features)
        _validate_refit(refit, candidate, candidate_policy, declared_features, protected_ids)
    except Exception as error:
        # Do not serialize exception text: a failed dependency must not leak its
        # inputs into the steering audit trail.
        return _outcome(scorecard, policy, scorecard.fingerprint, True, False, None, proposal, proposal_fingerprint,
                        "fit-failed", candidate.fingerprint, type(error).__name__)

    objective = _finite_objective(
        "candidate development objective", development_objective(candidate, candidate_policy, refit)
    )
    if objective <= incumbent:
        return _outcome(scorecard, policy, scorecard.fingerprint, True, False, None, proposal, proposal_fingerprint,
                        "not-better-on-development", candidate.fingerprint, None)
    return _outcome(candidate, candidate_policy, scorecard.fingerprint, True, True, refit, proposal, proposal_fingerprint,
                    "promoted", candidate.fingerprint, None)


def _outcome(scorecard: Scorecard, policy: ContextPolicy, parent_scorecard_fingerprint: str, accepted: bool, promoted: bool,
             refit: LearnedHead | None, proposal: SteeringProposal, proposal_fingerprint: str, decision: str,
             candidate_fingerprint: str | None, reason: str | None) -> SteeringOutcome:
    return SteeringOutcome(scorecard, policy, accepted, promoted, refit, proposal,
                           (SteeringHistory(proposal_fingerprint, decision, parent_scorecard_fingerprint,
                                            candidate_fingerprint, reason),))


def steering_observations(outcome: SteeringOutcome) -> tuple[tuple[FeatureDefinition, ...], tuple[FeatureActivity, ...]]:
    """Adapt one real steering outcome to the reusable UI/ledger feature contract."""
    features = tuple(FeatureDefinition(element.key, element.question_type, element.feature_names,
                                       element.definition_fingerprint)
                     for element in outcome.scorecard.elements)
    proposal = outcome.proposal
    feature_key = (proposal.add_element.key if proposal.add_element is not None
                   else proposal.add_programmatic_element or proposal.context_policy_name or "context-policy")
    decision = outcome.history[-1].decision
    status = {"rejected": "rejected", "fit-failed": "fit-failed",
              "not-better-on-development": "not-promoted", "promoted": "promoted"}[decision]
    return features, (FeatureActivity(outcome.history[-1].proposal_fingerprint, feature_key, status,
                                      outcome.history[-1].reason, None),)


def _guard_initial_lineage(scorecard: Scorecard, policy: ContextPolicy) -> None:
    if scorecard.policy_fingerprint != policy.fingerprint:
        raise ValueError("scorecard and active policy fingerprint must match")


def _finite_objective(name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
        raise ValueError(f"{name} must be a finite numeric value")
    return float(value)


def _guard_protected(developer_ids, developer_hashes, protected_ids, protected_hashes) -> None:
    if set(developer_ids) & set(protected_ids) or set(developer_hashes) & set(protected_hashes):
        raise ValueError("protected scoreboard identifiers or hashes cannot enter analyst briefing")


def _parse_proposal(raw: Mapping[str, object], allowed_policies: Mapping[str, ContextPolicy]) -> SteeringProposal:
    if not isinstance(raw, Mapping):
        raise ValueError("analyst proposal must be an object")
    allowed = {"add_element", "add_programmatic_element", "context_policy"}
    unknown = set(raw) - allowed
    if unknown:
        raise ValueError("analyst proposal edit is not allowed")
    if len(raw) != 1:
        raise ValueError("analyst proposal must contain one allowed scorecard edit")
    if "context_policy" in raw:
        name = raw["context_policy"]
        if not isinstance(name, str) or name not in allowed_policies:
            raise ValueError("context policy proposal is not in the code allowlist")
        return SteeringProposal(context_policy_name=name)
    if "add_programmatic_element" in raw:
        detail = raw["add_programmatic_element"]
        if not isinstance(detail, Mapping) or set(detail) != {"kind"}:
            raise ValueError("add_programmatic_element must provide only kind")
        if detail["kind"] != CURRENT_DATETIME_ELEMENT:
            raise ValueError("programmatic element is not in the code allowlist")
        return SteeringProposal(add_programmatic_element=CURRENT_DATETIME_ELEMENT)
    detail = raw["add_element"]
    if not isinstance(detail, Mapping) or set(detail) != {"key", "question_type", "features"}:
        raise ValueError("add_element must provide only key, question_type, and features")
    features = detail["features"]
    if not isinstance(features, list) or not all(isinstance(value, str) and value for value in features):
        raise ValueError("add_element features must be non-empty strings")
    key = detail["key"]
    question_type = detail["question_type"]
    if not isinstance(key, str) or not key or not isinstance(question_type, str) or not question_type:
        raise ValueError("add_element key and question_type must be non-empty strings")
    if key == CURRENT_DATETIME_ELEMENT:
        raise ValueError("current_datetime must use add_programmatic_element")
    return SteeringProposal(Element(key, question_type, tuple(features), _proposal_fingerprint(detail)))


def _apply(scorecard: Scorecard, proposal: SteeringProposal, candidate_policy: ContextPolicy) -> Scorecard:
    element = proposal.add_element
    if proposal.add_programmatic_element == CURRENT_DATETIME_ELEMENT:
        element = Element(CURRENT_DATETIME_ELEMENT, CURRENT_DATETIME_QUESTION_TYPE,
                          (CURRENT_DATETIME_ELEMENT,), _proposal_fingerprint({"kind": CURRENT_DATETIME_ELEMENT}))
    if element is not None and element.key in {element.key for element in scorecard.elements}:
        raise ValueError("proposal cannot duplicate its element")
    elements = scorecard.elements if proposal.context_policy_name is not None else scorecard.elements + (element,)
    if any(element is None for element in elements):
        raise ValueError("proposal must contain an allowed scorecard edit")
    return Scorecard(scorecard.name, scorecard.version + 1, elements, candidate_policy.fingerprint, scorecard.fingerprint)


def _declared_features(scorecard: Scorecard) -> tuple[str, ...]:
    features = tuple(name for element in scorecard.elements for name in element.feature_names)
    if len(set(features)) != len(features):
        raise ValueError("candidate scorecard feature names must be globally unique")
    return features


def _validate_refit(refit: object, candidate: Scorecard, candidate_policy: ContextPolicy,
                    declared_features: tuple[str, ...], protected_ids: Sequence[str]) -> None:
    if not isinstance(refit, LearnedHead):
        raise ValueError("numerical fitter must return the core LearnedHead")
    if refit.feature_names != declared_features:
        raise ValueError("numerical fit must cover every candidate scorecard feature")
    if refit.provenance.scorecard_fingerprint != candidate.fingerprint:
        raise ValueError("numerical fit scorecard lineage does not match candidate")
    if refit.provenance.policy_fingerprint != candidate_policy.fingerprint:
        raise ValueError("numerical fit policy lineage does not match candidate")
    training_ids = set(refit.provenance.training_ids)
    development_ids = set(refit.provenance.development_ids)
    protected = set(protected_ids)
    if training_ids & development_ids:
        raise ValueError("numerical fit training IDs cannot overlap development IDs")
    if training_ids & protected:
        raise ValueError("protected scoreboard IDs cannot appear in numerical fit training")
    if not protected.issubset(refit.provenance.scoreboard_ids):
        raise ValueError("protected scoreboard IDs must be recorded in numerical fit provenance")


def _proposal_fingerprint(value: object) -> str:
    import hashlib
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
