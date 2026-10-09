"""Display/translation grouping only; raw diarization labels remain authoritative."""

UNKNOWN_SPEAKERS = {None, "", "UU", "SU"}


def grouping_speaker(raw_speaker, last_known):
    """Treat unknown speech as continuation of the last confirmed speaker.

    This is a provisional grouping, not speaker identification. Callers reset
    last_known at recognition-session boundaries and never learn it from partials.
    """
    if raw_speaker in UNKNOWN_SPEAKERS and last_known not in UNKNOWN_SPEAKERS:
        return last_known
    return raw_speaker


def same_speaker_group(left, right):
    """Compare resolved grouping labels, including initial unknown-only runs.

    An unknown run is a translation grouping, not evidence of a single person.
    Callers must still enforce session and explicit paragraph boundaries.
    """
    return left == right or (left in UNKNOWN_SPEAKERS and right in UNKNOWN_SPEAKERS)
