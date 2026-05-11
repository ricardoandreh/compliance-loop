package ebs_encryption_default

default allow := false

allow if {
    input.resource.EbsEncryptionByDefault == true
}
