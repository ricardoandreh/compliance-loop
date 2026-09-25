package s3_encryption

default allow := false

# Every server-side encryption S3 offers, from SSE-S3 to dual-layer SSE-KMS.
# v1.0 listed only AES256 and aws:kms, so a bucket encrypted with DSSE-KMS —
# the strongest option — was classified as a violation, and the remediation
# would have rewritten it to AES256: the loop would have downgraded encryption
# while reporting convergence. Found by the policy's own unit tests.
accepted := {"AES256", "aws:kms", "aws:kms:dsse"}

allow if {
    some rule in input.resource.Rules
    rule.ApplyServerSideEncryptionByDefault.SSEAlgorithm in accepted
}
