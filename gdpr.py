"""GDPR data handler for stapel-analytics.

Analytics rows are user data. A behavioural stream keyed to a person is
personal data whether or not a name appears in it, and hashing the user id
is pseudonymisation, not anonymisation — so this module answers the erasure
protocol from its first release rather than acquiring a provider later.

Two protocols reach the same code (``erasure.py``):

- the **bus protocol** (stapel-gdpr 0.5.0+) — subscribed in ``apps.py`` via
  ``stapel_core.gdpr.register_gdpr_owner("analytics", …)``, which supplies
  the ``gdpr.erasure.requested`` / ``gdpr.owner.probe`` / legacy
  ``user.deleted`` handlers every owner library used to copy by hand;
- this **in-process provider** (monolith / export archive mode), which the
  host registers in ``STAPEL_GDPR["PROVIDERS"]`` and the DSAR export walks.

The policy is DELETE, not anonymize, and that asymmetry with stapel-docs is
deliberate: a document is co-produced workspace content that survives its
author, while an analytics row IS the author's behaviour and nothing else.
Stripping its subject would leave a timestamped record of one person's
session kept for the sake of a count.
"""
from __future__ import annotations

import logging

from stapel_core.gdpr import GDPRProvider

logger = logging.getLogger(__name__)


class AnalyticsGDPRProvider(GDPRProvider):
    section = "analytics"

    def export(self, user_id) -> dict:
        """Every stored row of this person, plus the hash they are keyed by.

        The hash is exported alongside the rows on purpose: it is the only
        thing that lets the person (or a regulator) verify that the export
        is complete and that the deletion afterwards was of the same
        subject.
        """
        from .erasure import export_account

        return export_account(user_id)

    def delete(self, user_id) -> dict:
        """Hard-delete this person's rows and the anonymous sessions linked
        to them. Idempotent — a second call finds nothing and says zeros."""
        from .erasure import erase_account

        return erase_account(user_id)

    def anonymize(self, user_id) -> dict:
        """Same as :meth:`delete`.

        There is no useful anonymize for this module (see the module
        docstring), and answering the anonymize protocol with a no-op would
        let a host believe it had de-identified a stream it had not.
        """
        return self.delete(user_id)


def erase_subject(subject_type: str, subject_key, workspace_id=None):
    """``register_gdpr_owner`` entry point: erase one subject, count the rows.

    Returns ``None`` for a subject type this owner does not claim — an
    erasure the orchestrator is not waiting for is not ours to confirm.
    """
    from .erasure import SUBJECT_TYPES, erase

    if subject_type not in SUBJECT_TYPES:
        return None
    return erase(subject_type, subject_key)


__all__ = ["AnalyticsGDPRProvider", "erase_subject"]
