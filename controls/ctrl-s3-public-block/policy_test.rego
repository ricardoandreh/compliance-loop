package s3_public_access_test

import data.s3_public_access

# Input shaped as the Analyze feeds it: the GetPublicAccessBlock response,
# under resource, with the PublicAccessBlockConfiguration already unwrapped.
all_true := {"BlockPublicAcls": true, "IgnorePublicAcls": true,
	"BlockPublicPolicy": true, "RestrictPublicBuckets": true}

test_all_four_settings_enabled if {
	s3_public_access.allow with input as {"resource": all_true}
}

# Each setting alone is enough to deny: a bucket is only compliant when all four
# are on, because each one closes a different path to public exposure.
test_any_single_setting_disabled_is_a_violation if {
	every setting in object.keys(all_true) {
		not s3_public_access.allow with input as {"resource": object.union(all_true, {setting: false})}
	}
}

# A bucket with no configuration at all comes back from the fetcher as the
# error_defaults (all false) — but a missing key must also deny on its own,
# never be read as "not disabled".
test_missing_setting_is_a_violation if {
	not s3_public_access.allow with input as {"resource": object.remove(all_true, ["RestrictPublicBuckets"])}
}

test_empty_configuration_is_a_violation if {
	not s3_public_access.allow with input as {"resource": {}}
}

# The API returns booleans; a string "true" from a hand-built input is not the
# same thing and must not pass.
test_string_true_is_not_true if {
	not s3_public_access.allow with input as {"resource": object.union(all_true, {"BlockPublicAcls": "true"})}
}
