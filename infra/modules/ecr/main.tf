variable "project_name" { type = string }
variable "environment" { type = string }

resource "aws_ecr_repository" "execute_lambda" {
  name                 = "${var.project_name}-execute-lambda"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration { scan_on_push = true }
  encryption_configuration { encryption_type = "AES256" }

  tags = { Name = "${var.project_name}-execute-lambda", Environment = var.environment }
}

resource "aws_ecr_lifecycle_policy" "execute_lambda" {
  repository = aws_ecr_repository.execute_lambda.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep only the last 5 images"
      selection    = { tagStatus = "any", countType = "imageCountMoreThan", countNumber = 5 }
      action       = { type = "expire" }
    }]
  })
}

output "repository_url" { value = aws_ecr_repository.execute_lambda.repository_url }
output "repository_arn" { value = aws_ecr_repository.execute_lambda.arn }
output "repository_name" { value = aws_ecr_repository.execute_lambda.name }
