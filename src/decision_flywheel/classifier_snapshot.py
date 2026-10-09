"""Classifier snapshots for events, without the fit proof that grows with the square of the training set."""
from copy import deepcopy
from dataclasses import asdict, fields, is_dataclass, replace

# Each out-of-fold row records the IDs, labels and normalizers it was fitted on,
# so this proof grows with the square of the training set. It is recorded once,
# in the fit-completed event of the fit that produced the head (and in the
# runtime's saved active state), not again in every event that names the head.
PER_ITEM_FIT_PROOF = ('fit_ids', 'fit_labels', 'normalization_fit_ids', 'normalizers')


def _plain(value):
    return asdict(value) if is_dataclass(value) else deepcopy(value)


def compact_snapshot(classifier):
    """The classifier for an event, without the per-item out-of-fold fit proof."""
    head = classifier.head
    snapshot = asdict(replace(classifier, head=None))
    if head is None:
        return snapshot
    snapshot['head'] = {field.name: _plain(getattr(head, field.name)) for field in fields(head)
                        if field.name != 'out_of_fold'}
    oof = head.out_of_fold
    snapshot['head']['out_of_fold'] = {
        **{field.name: _plain(getattr(oof, field.name)) for field in fields(oof)
           if field.name not in PER_ITEM_FIT_PROOF},
        'per_item_fit_proof': {'omitted': list(PER_ITEM_FIT_PROOF),
                               'recorded_in': 'fit-completed',
                               'head_fingerprint': head.refitted_cyclotron_fingerprint}}
    return snapshot
