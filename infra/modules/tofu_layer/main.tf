variable "project_name" { type = string }
variable "environment" { type = string }

resource "aws_lambda_layer_version" "tofu" {
  layer_name               = "${var.project_name}-tofu-binary-${var.environment}"
  filename                 = "${path.module}/../../../lambdas/execute/tofu_layer.zip"
  source_code_hash         = filebase64sha256("${path.module}/../../../lambdas/execute/tofu_layer.zip")
  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["arm64"]
  description              = "OpenTofu binary for remediation execution"
}

output "layer_arn" { value = aws_lambda_layer_version.tofu.arn }
