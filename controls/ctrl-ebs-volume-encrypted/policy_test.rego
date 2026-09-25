package ebs_volume_encrypted_test

import data.ebs_volume_encrypted

# Input shaped as DescribeVolumes answers for one volume ("Volumes.0").
volume(encrypted) := {"resource": {
	"VolumeId": "vol-0123456789abcdef0",
	"Encrypted": encrypted,
	"State": "available",
	"Size": 1,
}}

test_encrypted_volume if {
	ebs_volume_encrypted.allow with input as volume(true)
}

test_unencrypted_volume_is_a_violation if {
	not ebs_volume_encrypted.allow with input as volume(false)
}

# A volume encrypted with a customer key is as compliant as one with aws/ebs:
# this control asks for encryption, not for a particular key.
test_customer_managed_key_is_compliant if {
	ebs_volume_encrypted.allow with input as {"resource": object.union(volume(true).resource,
		{"KmsKeyId": "arn:aws:kms:us-east-1:123456789012:key/abcd"})}
}

test_missing_field_is_a_violation if {
	not ebs_volume_encrypted.allow with input as {"resource": {"VolumeId": "vol-0123456789abcdef0"}}
}
