"""Store adapter — the sole owner of all ClickHouse access and DDL (AD-18).

Story 1.1 provides only the connection factory + connection check
(``client.py``). Schema/DDL (Tier 0, Tier 1, Resolution MV, wide mart) lands in
Story 1.2.
"""
