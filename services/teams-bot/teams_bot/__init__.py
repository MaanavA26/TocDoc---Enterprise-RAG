"""TocDoc Microsoft Teams bot adapter (P4-1).

A thin Bot Framework adapter that speaks the Bot Framework protocol on its
inbound edge and the existing TocDoc QnA Azure AD JWT contract on its outbound
edge. See ``docs/architect_phase_2/10_P4_1_TEAMS_BOT_ADR.md`` and the package
README for the design and the unspoofable identity -> bot_tag model.
"""

from __future__ import annotations

__version__ = "0.1.0"
