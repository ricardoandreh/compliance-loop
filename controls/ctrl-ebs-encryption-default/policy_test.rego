package ebs_encryption_default_test

import data.ebs_encryption_default

test_enabled if {
	ebs_encryption_default.allow with input as {"resource": {"EbsEncryptionByDefault": true}}
}

test_disabled_is_a_violation if {
	not ebs_encryption_default.allow with input as {"resource": {"EbsEncryptionByDefault": false}}
}

test_missing_field_is_a_violation if {
	not ebs_encryption_default.allow with input as {"resource": {}}
}

# The account-level SSE type rides along in the same response; it does not
# change the answer on its own.
test_sse_type_does_not_matter if {
	ebs_encryption_default.allow with input as {"resource": {"EbsEncryptionByDefault": true, "SseType": "sse-ebs"}}
}
