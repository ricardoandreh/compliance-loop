package s3_encryption

default allow := false

allow if {
    some rule in input.resource.Rules
    rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm == "AES256"
}

allow if {
    some rule in input.resource.Rules
    rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm == "aws:kms"
}
