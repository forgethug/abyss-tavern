"""Backward-compatible entry point for S1 narrative lease integrity repair."""

from migrations.repair_s1_narrative_lease_integrity import migrate


if __name__ == '__main__':
    migrate()
