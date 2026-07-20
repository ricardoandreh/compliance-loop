package s3_ssl_only_test

import data.s3_ssl_only

# Input shaped exactly as Analyze feeds the policy: the bucket policy arrives
# as a JSON *string* under resource.Policy, not as a parsed object.
bucket_input(stmts) := {"resource": {"Policy": json.marshal({
	"Version": "2012-10-17",
	"Statement": stmts,
})}}

# Baseline SSL-only deny in the legacy scalar form; variants are built from it.
deny := {
	"Effect": "Deny",
	"Condition": {"Bool": {"aws:SecureTransport": "false"}},
	"Action": "s3:*",
	"Principal": "*",
	"Resource": "*",
}

# --- Compliant policies (allow) -------------------------------------------

test_legacy_scalar_form if {
	s3_ssl_only.allow with input as bucket_input([deny])
}

# The AWS console default: Principal as {"AWS": "*"} and Action as a list.
test_console_form if {
	s3_ssl_only.allow with input as bucket_input([object.union(deny, {
		"Principal": {"AWS": "*"},
		"Action": ["s3:*"],
	})])
}

test_boolean_secure_transport if {
	s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Condition": {"Bool": {"aws:SecureTransport": false}}})])
}

test_action_list_with_extras if {
	s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Action": ["s3:GetObject", "s3:*"]})])
}

test_principal_aws_list if {
	s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Principal": {"AWS": ["*"]}})])
}

test_deny_among_other_statements if {
	s3_ssl_only.allow with input as bucket_input([
		{"Effect": "Allow", "Action": "s3:GetObject", "Principal": "*", "Resource": "*"},
		deny,
	])
}

# --- Non-compliant policies (deny) ----------------------------------------

test_empty_statements if {
	not s3_ssl_only.allow with input as bucket_input([])
}

test_secure_transport_true if {
	not s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Condition": {"Bool": {"aws:SecureTransport": "true"}}})])
}

test_missing_condition if {
	not s3_ssl_only.allow with input as bucket_input([object.remove(deny, ["Condition"])])
}

test_effect_allow if {
	not s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Effect": "Allow"})])
}

test_narrow_action_list if {
	not s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Action": ["s3:GetObject"]})])
}

test_specific_principal if {
	not s3_ssl_only.allow with input as bucket_input([object.union(deny, {"Principal": {"AWS": "arn:aws:iam::123456789012:root"}})])
}
