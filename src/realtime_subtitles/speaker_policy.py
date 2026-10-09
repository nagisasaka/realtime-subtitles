"""Grouping eligibility, without inventing speaker identities for unlabelled speech."""

UNKNOWN_SPEAKERS = {None, "", "UU", "SU"}


def same_speaker_group(left, right):
    """Group adjacent unknowns; keep known/unknown and known-speaker boundaries.

    An unknown run is a translation grouping, not evidence of a single person.
    Callers must still enforce session and explicit paragraph boundaries.
    """
    return left == right or (left in UNKNOWN_SPEAKERS and right in UNKNOWN_SPEAKERS)
