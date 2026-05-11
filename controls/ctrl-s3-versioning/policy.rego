package s3_versioning

default allow := false

allow if {
    input.resource.Status == "Enabled"
}
