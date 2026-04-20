package s3_ssl_only

default allow := false

allow if {
    some statement in input.resource.policy_statements
    statement.Effect == "Deny"
    statement.Condition.Bool["aws:SecureTransport"] == "false"
    statement.Action == "s3:*"
    statement.Principal == "*"
}
