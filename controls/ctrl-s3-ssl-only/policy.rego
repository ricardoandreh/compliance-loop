package s3_ssl_only

default allow := false

allow if {
    policy := json.unmarshal(input.resource.Policy)
    some statement in policy.Statement
    statement.Effect == "Deny"
    statement.Condition.Bool["aws:SecureTransport"] == "false"
    statement.Action == "s3:*"
    statement.Principal == "*"
}
