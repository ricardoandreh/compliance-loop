package s3_ssl_only

default allow := false

# A bucket is compliant when its policy contains a statement that denies every
# S3 action, for every principal, whenever the request does not use TLS.
#
# Each field is matched across the equivalent shapes the IAM/S3 policy grammar
# allows. The previous version compared Action, Principal and the
# aws:SecureTransport value as scalars only, so a perfectly valid SSL-only
# policy authored in any other canonical form was misreported as a violation
# (false negative) — notably the AWS console default, which renders the
# principal as {"AWS": "*"}. Those false negatives trigger spurious remediation
# and pollute the compliance log.
allow if {
	policy := json.unmarshal(input.resource.Policy)
	some statement in policy.Statement
	statement.Effect == "Deny"
	denies_insecure_transport(statement)
	action_covers_all(statement.Action)
	principal_is_all(statement.Principal)
}

# Condition Bool aws:SecureTransport is false — tolerate the string "false"
# (most common) and the boolean false that some tools emit.
denies_insecure_transport(statement) if {
	is_false(statement.Condition.Bool["aws:SecureTransport"])
}

is_false(v) if v == "false"

is_false(v) if v == false

# Action is s3:* whether provided as a scalar or inside a list.
action_covers_all(action) if action == "s3:*"

action_covers_all(action) if {
	is_array(action)
	"s3:*" in action
}

# Principal "*" as a scalar, or the object form {"AWS": "*"} /
# {"AWS": ["*", ...]} that the console and many tools generate.
principal_is_all(principal) if principal == "*"

principal_is_all(principal) if {
	is_object(principal)
	principal.AWS == "*"
}

principal_is_all(principal) if {
	is_object(principal)
	is_array(principal.AWS)
	"*" in principal.AWS
}
