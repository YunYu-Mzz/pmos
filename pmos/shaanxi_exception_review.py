"""Offline derivation for completed originals; never replay a failed request."""
import copy

BROWSE_HISTORY='/px-settlement-infpubmeex/ipMemberBrowseHistory/addOneStructured'


def completed_receipt(original):
    receipt=copy.deepcopy(original)
    if receipt.get('pending') or receipt.get('failures') or receipt.get('blockingFailures') or receipt.get('bodyReadsPending') or receipt.get('error') or receipt.get('invokeError') or receipt.get('foreignFrameRequests'):
        raise ValueError('receipt_not_complete_for_offline_recovery')
    if receipt.get('phase')=='held':
        unknown=[x for x in receipt.get('responses',[]) if x.get('kind')=='unreviewed']
        if not unknown or any(x.get('path')!=BROWSE_HISTORY or x.get('status')!=200 or x.get('businessStatus')!=0 for x in unknown):
            raise ValueError('only_completed_native_browse_history_is_reviewed')
        if any(x.get('status')!=200 or x.get('securityDocument') or x.get('kind')=='native_sdk_auth' and x.get('businessStatus')!=0 for x in receipt['responses']):
            raise ValueError('refusal_or_auth_error_cannot_be_recovered')
        for x in unknown:x['kind']='native_auxiliary';x['reviewedOriginalKind']='unreviewed'
        receipt['phase']='captured_requires_adapter_validation'
        receipt['adapterReview']={'reason':'completed_native_browse_history_is_auxiliary','websiteRequestsIssued':0,'originalReceiptUnchanged':True}
    elif receipt.get('phase') not in {'captured_requires_adapter_validation','discovered_requires_adapter_validation'}:
        raise ValueError('completed_native_capture_required')
    return receipt
