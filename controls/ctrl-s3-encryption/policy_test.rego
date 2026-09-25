package s3_encryption_test

import data.s3_encryption

# GetBucketEncryption answers with ServerSideEncryptionConfiguration.Rules; the
# fetcher's result_path unwraps it down to the Rules list's parent.
with_algorithm(algorithm) := {"resource": {"Rules": [
	{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": algorithm}},
]}}

test_sse_s3 if {
	s3_encryption.allow with input as with_algorithm("AES256")
}

test_sse_kms if {
	s3_encryption.allow with input as with_algorithm("aws:kms")
}

# Dual-layer SSE-KMS is the strongest option S3 offers. Classifying it as a
# violation would make the loop "remediate" it down to AES256 — a downgrade.
test_dsse_kms_is_compliant if {
	s3_encryption.allow with input as with_algorithm("aws:kms:dsse")
}

test_no_rules_is_a_violation if {
	not s3_encryption.allow with input as {"resource": {"Rules": []}}
}

test_unknown_algorithm_is_a_violation if {
	not s3_encryption.allow with input as with_algorithm("none")
}

# Any rule that encrypts is enough; an extra rule without a default does not
# undo it.
test_one_encrypting_rule_among_several if {
	s3_encryption.allow with input as {"resource": {"Rules": [
		{"BucketKeyEnabled": true},
		{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}},
	]}}
}
