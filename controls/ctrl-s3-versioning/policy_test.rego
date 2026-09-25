package s3_versioning_test

import data.s3_versioning

test_enabled if {
	s3_versioning.allow with input as {"resource": {"Status": "Enabled"}}
}

# Suspended keeps the old versions but stops creating new ones: not compliant.
test_suspended_is_a_violation if {
	not s3_versioning.allow with input as {"resource": {"Status": "Suspended"}}
}

# A bucket that never had versioning answers GetBucketVersioning with no Status.
test_never_enabled_is_a_violation if {
	not s3_versioning.allow with input as {"resource": {}}
}

# MFA delete is a separate setting; enabling it does not make versioning on.
test_mfa_delete_alone_is_a_violation if {
	not s3_versioning.allow with input as {"resource": {"MFADelete": "Enabled"}}
}

test_status_is_case_sensitive if {
	not s3_versioning.allow with input as {"resource": {"Status": "enabled"}}
}
