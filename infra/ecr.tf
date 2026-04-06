# ECR Repository for Execute Lambda Container Image

resource "aws_ecr_repository" "execute_lambda" {
  name                 = "${var.project_name}-execute-lambda"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Name        = "${var.project_name}-execute-lambda"
    Environment = var.environment
    Component   = "execute-lambda"
  }
}

# Lifecycle policy to keep only the last 5 images
resource "aws_ecr_lifecycle_policy" "execute_lambda" {
  repository = aws_ecr_repository.execute_lambda.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep only the last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}
