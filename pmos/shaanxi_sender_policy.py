"""Permit a later fresh report after a proven pre-AUTH TLS EOF.

The original sender never resubmits that report. Certificate failures and
uncertain AUTH/DATA phases retain its existing manual-review boundary.
"""
import ssl

def connection_failure_before_submission(original,receipt,error):
    return original(receipt,error) or (receipt.get('stage')=='smtp_connecting' and isinstance(error,ssl.SSLEOFError))
