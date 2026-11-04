import ssl,unittest
from pmos.shaanxi_sender_policy import connection_failure_before_submission

class FreshReportOnly(unittest.TestCase):
    def test_eof_before_smtp_client_return_allows_only_a_fresh_report(self):
        self.assertTrue(connection_failure_before_submission(lambda *_:False,{'stage':'smtp_connecting'},ssl.SSLEOFError(8,'fixture')))
    def test_auth_or_data_uncertainty_still_requires_review(self):
        for stage in ['smtp_authenticating','smtp_submitting','smtp_accepted',None]:
            self.assertFalse(connection_failure_before_submission(lambda *_:False,{'stage':stage},ssl.SSLEOFError(8,'fixture')))
    def test_certificate_failure_is_never_relaxed(self):
        self.assertFalse(connection_failure_before_submission(lambda *_:False,{'stage':'smtp_connecting'},ssl.SSLCertVerificationError(1,'fixture')))
    def test_original_classification_is_preserved(self):
        self.assertTrue(connection_failure_before_submission(lambda *_:True,{},TimeoutError()))

if __name__=='__main__':unittest.main()
