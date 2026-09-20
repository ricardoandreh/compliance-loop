package ebs_volume_encrypted

default allow := false

# Detective control: an existing volume cannot be encrypted in place, so the
# loop never remediates this one — it classifies and escalates (control.json
# carries "remediation": "manual"). Encryption by default (ctrl-ebs-encryption-default)
# only covers volumes created after it was enabled; this covers the volumes
# themselves (design.md, L14).
allow if {
    input.resource.Encrypted == true
}
