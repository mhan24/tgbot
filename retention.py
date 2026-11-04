# coding: utf-8
"""Explicit reading times for group notices; interactive cards define their own lifetime."""

# Failures, warnings and punishment notices stay long enough to read. Callers name
# the category explicitly; message wording is never used to guess a lifetime.
NOTICE_SECONDS = 120
