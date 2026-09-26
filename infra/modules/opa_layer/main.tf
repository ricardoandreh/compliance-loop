variable "project_name" { type = string }
variable "environment" { type = string }

resource "aws_lambda_layer_version" "opa" {
  layer_name               = "${var.project_name}-opa-binary-${var.environment}"
  filename                 = "${path.module}/../../../lambdas/analyze/opa_layer.zip"
  source_code_hash         = filebase64sha256("${path.module}/../../../lambdas/analyze/opa_layer.zip")
  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["arm64"]
  description              = "OPA binary for policy evaluation"
}

output "layer_arn" { value = aws_lambda_layer_version.opa.arn }
