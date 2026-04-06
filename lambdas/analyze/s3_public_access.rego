package s3_public_access

default allow := false

allow if {
    input.resource.BlockPublicAcls == true
    input.resource.IgnorePublicAcls == true
    input.resource.BlockPublicPolicy == true
    input.resource.RestrictPublicBuckets == true
}
