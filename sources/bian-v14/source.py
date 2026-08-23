#!/usr/bin/env python3
"""
BIAN Service Landscape v14.

Everything that could differ between landscape versions is here; everything
that does not is in bianlib/. To add v13, copy this directory to
sources/bian-v13/, change the four constants at the top and re-run the numbers
in Sanity below against that landscape — counts and object ids differ between
versions, so a canary borrowed from v14 will not resolve in v13.

The two versions then harvest independently into out/bian-v14/ and
out/bian-v13/, and publish independently to gdrive:content/bian-v14/ and
gdrive:content/bian-v13/. Neither sync can reach the other's folder.

Credentials: none. The files are served unauthenticated.
"""

from __future__ import annotations

from bianlib.source import BianSource


class Source(BianSource):
    id = "bian-v14"
    name = "BIAN Service Landscape v14"
    description = "Banking Industry Architecture Network service domain model"

    # --- version pinning ---------------------------------------------
    # When BIAN publishes a new landscape you receive a fresh link by email.
    # Update these, then run "Validate — BIAN v14" before anything else.
    base = "https://bian.org/servicelandscape-14-0-0"
    object_view = 16
    last_known_shard = 47
    # -----------------------------------------------------------------

    # --- sanity: verified against v14.0, August 2026 ------------------
    # Canary: a known service domain. If BIAN restructures or the parser
    # drifts, validation fails loudly instead of quietly producing thinner
    # output.
    canary_id = "34300"
    canary_name = "Consumer Loan"

    # The full model holds more service domains than the value chain view
    # depicts (367 vs 340 on views/view_54486.html), since not every domain
    # appears on that diagram. A result near 222 means only one shard is being
    # read — that was the original bug, and it was silent.
    expected_service_domains = 367
    min_service_domains = 340
    # After allowlist filtering, roughly 11,300 of 128,270 objects remain.
    min_objects = 8000
    # 429 sequence and 802 class diagrams exist at v14.0. The minimums allow
    # for a handful of pages legitimately failing to convert.
    min_sequence_diagrams = 400
    min_class_diagrams = 760
    # What the classifier should see before any view page is fetched.
    expected_sequence_views = 429
    expected_class_views = 802
    min_views = 1500
    # -----------------------------------------------------------------

    schedule = "0 3 * * 1"
